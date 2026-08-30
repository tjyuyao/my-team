"""Calendar scheduler: ScheduleRule evaluation (SPEC §9.1, T11).

时间模型（SPEC §9.1）: rules are expressed in business real time —
a day/week cron subset (daily / weekly-at-HH:MM) — or in engine
cadence (``interval_ticks``). The engine checks due-ness each tick
with ``wall_now()``; rule advancement is a staged ``RULE_ADVANCE``
effect committed atomically with any task the rule creates, so a
rolled-back tick neither loses nor double-fires a rule (T11 决策 1).

Cron subset (T11 决策 4): structured fields, no string cron parsing.

N1c-3 设备归位：CalendarStore 继承 Device，注册日历数据面受控实体；
到期判定/RULE_ADVANCE 算法留内核（Simulation._check_calendar）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from my_team.devices.base import Device, EntityKind, InjectionDecl
from my_team.models.task import TaskPriority
from pydantic import BaseModel, Field, model_validator


class CronSpec(BaseModel):
    """Day/week cron subset (T11 决策 4).

    ``freq="daily"`` fires every day at ``at_time``;
    ``freq="weekly"`` fires on the given ISO weekdays
    (0=Monday … 6=Sunday) at ``at_time``.
    """

    freq: Literal["daily", "weekly"]
    days_of_week: tuple[int, ...] = Field(
        default_factory=tuple,
        description="ISO weekdays (0=Mon..6=Sun); weekly only",
    )
    at_time: str = Field(description='Time of day "HH:MM" (00:00-23:59)')

    @model_validator(mode="after")
    def _validate(self) -> CronSpec:
        parts = self.at_time.split(":")
        if len(parts) != 2:
            raise ValueError(f"at_time must be HH:MM, got {self.at_time!r}")
        hour, minute = (int(p) for p in parts)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"at_time out of range: {self.at_time!r}")
        if self.freq == "weekly":
            if not self.days_of_week:
                raise ValueError("weekly cron requires days_of_week")
            if any(not 0 <= d <= 6 for d in self.days_of_week):
                raise ValueError("days_of_week must be ISO 0..6")
        return self

    def next_fire_after(self, after: datetime) -> datetime:
        """Next scheduled occurrence strictly after ``after``.

        Pure function of the spec and the base time — deterministic
        and replayable. Naive datetimes are treated as UTC by the
        caller (Simulation passes wall_now(), which is aware).
        """
        hour, minute = (int(p) for p in self.at_time.split(":"))
        candidate = after.replace(hour=hour, minute=minute, second=0,
                                  microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        if self.freq == "daily":
            return candidate
        # weekly: advance day-by-day until the weekday matches.
        wanted = set(self.days_of_week)
        for _ in range(8):  # at most 7 days + the first hop
            if candidate.weekday() in wanted:
                return candidate
            candidate += timedelta(days=1)
        raise ValueError("unreachable: no matching weekday within 8 days")


class ScheduleAction(str, Enum):
    """What a due rule does (SPEC §9.1)."""

    CREATE_TASK = "create_task"
    EMIT_EVENT = "emit_event"


class TaskTemplate(BaseModel):
    """Task created when a CREATE_TASK rule fires."""

    title: str
    description: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    deadline_offset_minutes: int | None = Field(
        default=None,
        description=(
            "Real-time offset from the scheduled fire time to the "
            "created task's deadline (business real time, SPEC §9.1)"
        ),
    )


class ScheduleRule(BaseModel):
    """A calendar rule evaluated each tick (SPEC §9.1).

    Exactly one of ``cron`` / ``interval_ticks``. ``next_run_tick`` is
    engine bookkeeping for interval rules; ``last_fired_at`` records
    the business time of the last fire (cron). Both are advanced only
    via the committed RULE_ADVANCE effect (T11 决策 1).
    """

    rule_id: str
    target_agent_id: str = Field(
        description="Agent that owns tasks/events produced by this rule",
    )
    cron: CronSpec | None = None
    interval_ticks: int | None = Field(default=None, ge=1)
    action: ScheduleAction = ScheduleAction.CREATE_TASK
    task_template: TaskTemplate | None = None
    enabled: bool = True
    next_run_tick: int = Field(default=0, ge=0)
    last_fired_at: datetime | None = None
    registered_at: datetime | None = Field(
        default=None,
        description=(
            "Business time of registration; the first cron fire is the "
            "next occurrence strictly after this (no catch-up)"
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> ScheduleRule:
        if (self.cron is None) == (self.interval_ticks is None):
            raise ValueError(
                "exactly one of cron / interval_ticks is required",
            )
        if self.action == ScheduleAction.CREATE_TASK and (
            self.task_template is None
        ):
            raise ValueError("create_task rules require task_template")
        return self


class CalendarStore(Device):
    """日历规则注册表（SPEC §9.1 数据面 + N1c-3 设备归位）。

    - 数据面：CronSpec/ScheduleRule 注册/advance/restore；
    - 行为面（到期判定/RULE_ADVANCE 算法）留内核（Simulation._check_calendar）；
    - 继承 Device 后注册范围级 DATA 实体（日历数据归位）。

    Registry of schedule rules with validation at registration
    (SPEC §11.2 加载即校验 discipline).
    """

    def __init__(self, device_id: str | None = None) -> None:
        # Device 基类初始化（注册受控实体）
        Device.__init__(self, device_id)
        self._rules: dict[str, ScheduleRule] = {}
        # N1c-3：注册日历数据面受控实体（范围级 DATA）
        self.calendar_scope_id = self.register_entity(
            EntityKind.DATA,
            "calendar-scope",
            injection=InjectionDecl(
                content=(
                    "[CALENDAR_INSTRUCTION] 日历设备（CalendarDevice）持有调度规则数据面。\n"
                    "CronSpec/ScheduleRule 在此注册；到期判定与 RULE_ADVANCE 效果由内核完成。\n"
                    "调度规则支持每日/每周 cron 与 interval_ticks 两种触发方式。"
                ),
                source_tag="[CALENDAR_INSTRUCTION]",
            ),
        )

    def register(self, rule: ScheduleRule) -> ScheduleRule:
        if rule.rule_id in self._rules:
            raise ValueError(f"Schedule rule '{rule.rule_id}' already exists")
        self._rules[rule.rule_id] = rule
        return rule

    def get(self, rule_id: str) -> ScheduleRule:
        if rule_id not in self._rules:
            raise KeyError(f"Unknown schedule rule '{rule_id}'")
        return self._rules[rule_id]

    def exists(self, rule_id: str) -> bool:
        return rule_id in self._rules

    def enabled(self) -> list[ScheduleRule]:
        return [r for r in self._rules.values() if r.enabled]

    def all_rules(self) -> list[ScheduleRule]:
        return list(self._rules.values())

    def advance(
        self,
        rule_id: str,
        *,
        next_run_tick: int | None = None,
        last_fired_at: datetime | None = None,
    ) -> ScheduleRule:
        """Apply a committed RULE_ADVANCE (mutates schedule state)."""
        rule = self.get(rule_id)
        if next_run_tick is not None:
            rule.next_run_tick = next_run_tick
        if last_fired_at is not None:
            rule.last_fired_at = last_fired_at
        return rule

    def restore(
        self,
        rule_id: str,
        *,
        prev_next_run_tick: int,
        prev_last_fired_at: datetime | None,
    ) -> ScheduleRule:
        """Invert of advance (rollback, T18 RESTORE_PREVIOUS)."""
        rule = self.get(rule_id)
        rule.next_run_tick = prev_next_run_tick
        rule.last_fired_at = prev_last_fired_at
        return rule

    def __contains__(self, rule_id: str) -> bool:
        return rule_id in self._rules

    def __len__(self) -> int:
        return len(self._rules)
