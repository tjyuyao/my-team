"""Tests for T11 Calendar Scheduler (SPEC §9.1).

Covers:
- CronSpec.next_fire_after (daily / weekly, no catch-up)
- ScheduleRule validation (exactly one trigger; template required)
- Interval rules fire every N ticks; advancement committed (决策 1)
- Cron rules fire at real-calendar times against wall_now()
- EMIT_EVENT wakes enqueue post-commit only
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from my_team.agent_tree import AgentTree
from my_team.calendar import (
    CalendarStore,
    CronSpec,
    ScheduleAction,
    ScheduleRule,
    TaskTemplate,
)
from my_team.models.activation import WakeEventType
from my_team.models.task import TaskPriority

_BASE = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)  # a Friday


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write", "ls"],
                "can_delegate": True,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _make_sim(anchor: datetime = _BASE):
    from my_team.simulation import Simulation

    sim = Simulation(agent_tree=_make_tree())
    engine = sim.tick_engine
    engine._config.tick_duration_value = 10
    engine._config.tick_duration_unit = "minutes"
    engine._anchor = anchor
    return sim


class TestCronSpec:
    def test_daily_next_occurrence_same_day(self):
        cron = CronSpec(freq="daily", at_time="09:30")
        nxt = cron.next_fire_after(_BASE.replace(hour=9, minute=0))
        assert nxt == _BASE.replace(hour=9, minute=30)

    def test_daily_rolls_to_tomorrow_when_time_passed(self):
        cron = CronSpec(freq="daily", at_time="09:00")
        nxt = cron.next_fire_after(_BASE.replace(hour=9, minute=1))
        assert nxt == _BASE.replace(hour=9, minute=0) + timedelta(days=1)

    def test_weekly_skips_to_matching_weekday(self):
        # 2026-08-21 is a Friday (weekday 4); next Monday is 08-24.
        cron = CronSpec(freq="weekly", days_of_week=(0,), at_time="08:00")
        nxt = cron.next_fire_after(_BASE)
        assert nxt.weekday() == 0
        assert nxt == datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

    def test_weekly_multiple_days_picks_nearest(self):
        cron = CronSpec(freq="weekly", days_of_week=(0, 2), at_time="07:00")
        nxt = cron.next_fire_after(_BASE)  # Friday → next Monday or Wednesday
        assert nxt.weekday() in (0, 2)

    def test_validation_bad_time(self):
        with pytest.raises(ValueError):
            CronSpec(freq="daily", at_time="25:00")

    def test_validation_weekly_requires_days(self):
        with pytest.raises(ValueError):
            CronSpec(freq="weekly", at_time="09:00")


class TestScheduleRuleValidation:
    def test_exactly_one_trigger(self):
        with pytest.raises(ValueError):
            ScheduleRule(rule_id="r1", target_agent_id="agent.root")
        with pytest.raises(ValueError):
            ScheduleRule(
                rule_id="r1", target_agent_id="agent.root",
                cron=CronSpec(freq="daily", at_time="09:00"),
                interval_ticks=5,
            )

    def test_create_task_requires_template(self):
        with pytest.raises(ValueError):
            ScheduleRule(
                rule_id="r1", target_agent_id="agent.root",
                interval_ticks=5, action=ScheduleAction.CREATE_TASK,
            )


class TestCalendarStore:
    def test_register_duplicate_rejected(self):
        store = CalendarStore()
        rule = ScheduleRule(
            rule_id="r1", target_agent_id="a",
            interval_ticks=2, action=ScheduleAction.EMIT_EVENT,
        )
        store.register(rule)
        with pytest.raises(ValueError, match="already exists"):
            store.register(rule)

    def test_advance_restore_roundtrip(self):
        """决策 1: rollback restores prior schedule state."""
        store = CalendarStore()
        rule = ScheduleRule(
            rule_id="r1", target_agent_id="a",
            interval_ticks=2, action=ScheduleAction.EMIT_EVENT,
            next_run_tick=4,
        )
        store.register(rule)
        store.advance("r1", next_run_tick=6, last_fired_at=_BASE)
        assert rule.next_run_tick == 6
        store.restore(
            "r1", prev_next_run_tick=4, prev_last_fired_at=None,
        )
        assert rule.next_run_tick == 4
        assert rule.last_fired_at is None


class TestIntervalRuleIntegration:
    def test_interval_rule_creates_task_every_n_ticks(self):
        sim = _make_sim()
        sim.register_schedule_rule(ScheduleRule(
            rule_id="r.every2", target_agent_id="agent.root",
            interval_ticks=2, action=ScheduleAction.CREATE_TASK,
            task_template=TaskTemplate(
                title="Standup",
                priority=TaskPriority.HIGH,
                deadline_offset_minutes=30,
            ),
        ))
        results = [sim.run_tick() for _ in range(5)]
        assert all(r.committed for r in results)
        # Fired at ticks 0, 2, 4 → three tasks.
        tasks = [
            t for t in sim.task_tree.all_ids()
        ]
        assert len(tasks) == 3
        titles = sorted(
            sim.task_tree.get(tid).title for tid in tasks
        )
        assert titles == ["Standup", "Standup", "Standup"]
        # Advancement committed: next_run_tick == 6 after tick 4.
        rule = sim._calendar_store.get("r.every2")
        assert rule.next_run_tick == 6
        # Template fields propagated (priority + real-time deadline).
        any_task = sim.task_tree.get(tasks[0])
        assert any_task.priority == TaskPriority.HIGH
        assert any_task.deadline is not None

    def test_cron_rule_fires_at_real_calendar_time(self):
        sim = _make_sim()
        sim.register_schedule_rule(ScheduleRule(
            rule_id="r.daily", target_agent_id="agent.root",
            cron=CronSpec(freq="daily", at_time="09:30"),
            action=ScheduleAction.CREATE_TASK,
            task_template=TaskTemplate(title="Daily digest"),
        ))
        # Ticks at 09:00..09:40 (10 min each): due exactly at 09:30 (tick 3).
        for _ in range(6):
            sim.run_tick()
        rule = sim._calendar_store.get("r.daily")
        assert rule.last_fired_at == _BASE.replace(hour=9, minute=30)
        digest = [
            tid for tid in sim.task_tree.all_ids()
            if sim.task_tree.get(tid).title == "Daily digest"
        ]
        assert len(digest) == 1  # fired once, no catch-up duplicates

    def test_emit_event_wake_visible_next_tick(self):
        sim = _make_sim()
        sim.register_schedule_rule(ScheduleRule(
            rule_id="r.emit", target_agent_id="agent.root",
            interval_ticks=1, action=ScheduleAction.EMIT_EVENT,
        ))
        sim.run_tick()  # tick 0: fires; wake enqueued post-commit
        wakes = [
            qe.event for qe in sim._scheduler.all_events()
            if qe.event.event_type == WakeEventType.SCHEDULE_TRIGGER
        ]
        assert len(wakes) == 1
        assert wakes[0].details["rule_id"] == "r.emit"

    def test_unknown_target_rejected(self):
        sim = _make_sim()
        with pytest.raises(ValueError, match="unknown agent"):
            sim.register_schedule_rule(ScheduleRule(
                rule_id="r.bad", target_agent_id="agent.nope",
                interval_ticks=1, action=ScheduleAction.EMIT_EVENT,
            ))


def test_rule_advance_declared_in_invert_contract():
    """决策 1: every effect type has an invert; RULE_ADVANCE restores."""
    from my_team.transaction import INVERT_CONTRACT, EffectType, InvertKind

    spec = INVERT_CONTRACT[EffectType.RULE_ADVANCE]
    assert spec.kind == InvertKind.RESTORE_PREVIOUS
