"""Reliability mechanisms: timeout, retry, lock lease, deterministic replay.

Per SPEC §14:
- Agent execution failure: rollback, retry, block, notify
- Sub-task timeout: mark expired, notify owner
- Lock timeout: auto-release, audit, notify
- Email delivery failure: exponential backoff, max retries
- Deterministic replay: same input → same output
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from my_team.audit import AuditEventType, AuditLog
from my_team.shared_kb import LockManager
from my_team.task_tree import TaskTree
from pydantic import BaseModel, Field


class RetryPolicy(BaseModel):
    """Configuration for retry behavior."""

    max_retries: int = Field(default=3, description="Maximum retry attempts")
    base_delay_ticks: int = Field(default=1, description="Base delay in ticks")
    max_delay_ticks: int = Field(default=10, description="Maximum delay in ticks")
    backoff_multiplier: float = Field(default=2.0, description="Exponential backoff multiplier")


class FailureType(str, Enum):
    """Types of failures that can occur."""

    TOOL_ERROR = "tool_error"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    UNKNOWN = "unknown"


class FailureRecord(BaseModel):
    """Record of a failure event."""

    agent_id: str
    task_id: str
    failure_type: FailureType
    tick: int
    error: str
    retry_count: int = 0
    retryable: bool = True
    resolved: bool = False


class RetryManager:
    """Manages retry logic for failed operations.

    Implements exponential backoff and tracks retry state per agent/task.
    """

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._policy = policy or RetryPolicy()
        self._audit = audit_log if audit_log is not None else AuditLog()
        self._failures: dict[str, FailureRecord] = {}  # agent:task → record
        self._retry_counts: dict[str, int] = {}

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

    def _key(self, agent_id: str, task_id: str) -> str:
        return f"{agent_id}:{task_id}"

    def record_failure(
        self,
        agent_id: str,
        task_id: str,
        failure_type: FailureType,
        error: str,
        tick: int,
        retryable: bool = True,
    ) -> FailureRecord:
        """Record a failure and determine if retry is possible."""
        key = self._key(agent_id, task_id)
        count = self._retry_counts.get(key, 0)

        record = FailureRecord(
            agent_id=agent_id,
            task_id=task_id,
            failure_type=failure_type,
            tick=tick,
            error=error,
            retry_count=count,
            retryable=retryable and count < self._policy.max_retries,
        )
        self._failures[key] = record

        self._audit.record(
            AuditEventType.AGENT_FAILED,
            agent_id=agent_id,
            tick=tick,
            details={
                "task_id": task_id,
                "failure_type": failure_type.value,
                "error": error,
                "retry_count": count,
                "retryable": record.retryable,
            },
            success=False,
            error=error,
        )

        return record

    def can_retry(self, agent_id: str, task_id: str) -> bool:
        """Check if a failed operation can be retried."""
        key = self._key(agent_id, task_id)
        record = self._failures.get(key)
        if record is None:
            return False
        if not record.retryable:
            return False
        count = self._retry_counts.get(key, 0)
        return count < self._policy.max_retries

    def calculate_delay(self, agent_id: str, task_id: str) -> int:
        """Calculate retry delay with exponential backoff."""
        key = self._key(agent_id, task_id)
        count = self._retry_counts.get(key, 0)
        delay = self._policy.base_delay_ticks * (
            self._policy.backoff_multiplier ** count
        )
        return min(int(delay), self._policy.max_delay_ticks)

    def execute_retry(
        self,
        agent_id: str,
        task_id: str,
        tick: int,
    ) -> int:
        """Increment retry count and return the delay in ticks.

        Returns -1 if retry is not possible.
        """
        if not self.can_retry(agent_id, task_id):
            return -1

        key = self._key(agent_id, task_id)
        count = self._retry_counts.get(key, 0)

        delay = self.calculate_delay(agent_id, task_id)
        self._retry_counts[key] = count + 1

        self._audit.record(
            AuditEventType.AGENT_RETRY,
            agent_id=agent_id,
            tick=tick,
            details={
                "task_id": task_id,
                "retry_number": count + 1,
                "delay_ticks": delay,
            },
        )

        return delay

    def mark_resolved(self, agent_id: str, task_id: str) -> None:
        """Mark a failure as resolved."""
        key = self._key(agent_id, task_id)
        if key in self._failures:
            self._failures[key].resolved = True

    def get_failure(self, agent_id: str, task_id: str) -> FailureRecord | None:
        return self._failures.get(self._key(agent_id, task_id))

    def active_failures(self) -> list[FailureRecord]:
        return [f for f in self._failures.values() if not f.resolved]

    def __len__(self) -> int:
        return len([f for f in self._failures.values() if not f.resolved])


class TimeoutChecker:
    """Checks for and handles timeouts on tasks and locks.

    Per SPEC §14.2, §14.3:
    - Sub-task timeout: mark expired, notify owner
    - Lock timeout: auto-release, audit, notify
    """

    def __init__(
        self,
        task_tree: TaskTree,
        lock_manager: LockManager,
        audit_log: AuditLog,
    ) -> None:
        self._tasks = task_tree
        self._locks = lock_manager
        self._audit = audit_log

    def check_task_timeouts(self, now: datetime, tick: int) -> list[str]:
        """Check for and expire overdue tasks (real-calendar deadlines).

        Args:
            now: Current business wall-clock time (engine.wall_now()).
            tick: Current tick (for audit records and state stamps).

        Returns list of expired task IDs.
        """
        expired = self._tasks.get_expired_tasks(now)
        expired_ids: list[str] = []

        for task in expired:
            self._tasks.expire_task(task.task_id, tick)
            expired_ids.append(task.task_id)

            self._audit.record(
                AuditEventType.AGENT_FAILED,
                agent_id=task.assignee_agent_id,
                tick=tick,
                details={
                    "task_id": task.task_id,
                    "failure_type": "timeout",
                    "deadline": task.deadline.isoformat() if task.deadline else None,
                    "owner": task.assignee_agent_id,
                },
                success=False,
                error=(
                    f"Task expired at {now.isoformat()} "
                    f"(deadline: {task.deadline.isoformat() if task.deadline else None})"
                ),
            )

        return expired_ids

    def check_lock_timeouts(self, current_tick: int) -> list[dict[str, Any]]:
        """Check for and release expired locks.

        Returns list of released lock info dicts.
        """
        expired = self._locks.check_expired(current_tick)
        released: list[dict[str, Any]] = []

        for lock in expired:
            self._audit.record(
                AuditEventType.LOCK_EXPIRED,
                agent_id=lock.owner_agent_id,
                tick=current_tick,
                details={
                    "lock_id": lock.lock_id,
                    "resource": lock.resource,
                    "lease_until_tick": lock.lease_until_tick,
                },
            )
            released.append({
                "lock_id": lock.lock_id,
                "resource": lock.resource,
                "owner": lock.owner_agent_id,
            })

        return released

    def check_all(self, now: datetime, tick: int) -> dict[str, Any]:
        """Run all timeout checks. Returns summary."""
        expired_tasks = self.check_task_timeouts(now, tick)
        expired_locks = self.check_lock_timeouts(tick)

        return {
            "tick": tick,
            "now": now.isoformat(),
            "expired_tasks": expired_tasks,
            "expired_locks": expired_locks,
        }


class CrashReport(BaseModel):
    """Emergency ops report delivered to Provider/Owner callbacks when
    the crash guard triggers (T19)."""

    tick: int = Field(description="Tick at which the guard triggered")
    window_ticks: int = Field(description="Sliding window size (ticks)")
    threshold: int = Field(description="Crash count threshold within the window")
    crash_count: int = Field(description="Crash count inside the window at trigger")
    crash_ticks: list[int] = Field(description="Ticks with crashes inside the window")
    last_error: str = Field(default="", description="Most recent crash error")
    state_epoch: int = Field(default=0, description="State epoch at trigger")


class CrashGuard:
    """Detects repeated kernel-level crashes and auto-pauses the system.

    T19, user decision 2026-08-18: if the system itself keeps hitting
    kernel-level rollbacks / uncaught tick exceptions, that is a
    systemic defect — continuing to run would only spin, roll back, and
    pollute the Journal. A sliding window counts crash events; crossing
    the threshold: (1) fires Provider/Owner emergency callbacks FIRST,
    (2) pauses the system (reason='crash_guard') AFTER. Pausing never
    auto-resumes — a human must resume explicitly; resume() re-arms the
    guard (the window keeps sliding).

    Crash := a tick that rolled back at kernel level (unexpected apply
    exception) or a run_tick that raised uncaught. Deterministic
    business failures (local FAILED, T18 失败分级) are NOT crashes and
    must never be recorded here.
    """

    def __init__(
        self,
        window_ticks: int = 10,
        threshold: int = 3,
        audit_log: AuditLog | None = None,
        pause_action: Callable[[CrashReport], Any] | None = None,
    ) -> None:
        self._window_ticks = max(1, window_ticks)
        self._threshold = max(1, threshold)
        self._audit = audit_log if audit_log is not None else AuditLog()
        self._pause_action = pause_action
        self._crashes: deque[tuple[int, str]] = deque()  # (tick, error), ordered
        self._callbacks: dict[str, list[Callable[[CrashReport], Any]]] = {
            "provider": [],
            "owner": [],
        }
        self._triggered = False
        self._last_report: CrashReport | None = None

    # -- configuration ------------------------------------------------------

    @property
    def window_ticks(self) -> int:
        return self._window_ticks

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def triggered(self) -> bool:
        """True once the guard has fired (until rearmed by resume)."""
        return self._triggered

    @property
    def last_report(self) -> CrashReport | None:
        return self._last_report

    @property
    def crash_ticks(self) -> list[int]:
        """Ticks with crashes inside the current window."""
        return [t for t, _ in self._crashes]

    # -- callback registration ----------------------------------------------

    def register_emergency_callback(
        self,
        recipient: str,
        handler: Callable[[CrashReport], Any],
    ) -> None:
        """Register an emergency ops handler for 'provider' or 'owner'.

        Default is empty (log-only); tests inject probes to assert calls.
        """
        if recipient not in self._callbacks:
            raise ValueError(
                f"unknown emergency recipient '{recipient}' "
                "(expected 'provider' or 'owner')"
            )
        self._callbacks[recipient].append(handler)

    # -- crash recording ----------------------------------------------------

    def record_crash(
        self,
        tick: int,
        error: str,
        state_epoch: int = 0,
    ) -> CrashReport | None:
        """Record one kernel-level crash event.

        Returns the CrashReport when this event crossed the threshold
        and triggered the guard (else None). NEVER raise — the guard is
        a safety net on the failure path.
        """
        try:
            self._crashes.append((tick, error))
            # Evict crashes outside the sliding window.
            while (
                self._crashes
                and tick - self._crashes[0][0] >= self._window_ticks
            ):
                self._crashes.popleft()

            self._audit.record(
                AuditEventType.SYSTEM_CRASH,
                tick=tick,
                details={
                    "window_ticks": self._window_ticks,
                    "crash_ticks": self.crash_ticks,
                    "state_epoch": state_epoch,
                },
                success=False,
                error=error,
            )

            # Already triggered (awaiting human resume) — no re-fire.
            if self._triggered:
                return None
            if len(self._crashes) < self._threshold:
                return None

            self._triggered = True
            report = CrashReport(
                tick=tick,
                window_ticks=self._window_ticks,
                threshold=self._threshold,
                crash_count=len(self._crashes),
                crash_ticks=self.crash_ticks,
                last_error=error,
                state_epoch=state_epoch,
            )
            self._last_report = report

            # 决策 5: 先通知（Provider/Owner 回调），后暂停。
            for recipient in ("provider", "owner"):
                for handler in self._callbacks[recipient]:
                    try:
                        handler(report)
                    except Exception:  # noqa: BLE001 — a broken callback
                        pass   # must never break the guard

            self._audit.record(
                AuditEventType.CRASH_GUARD_TRIGGERED,
                tick=tick,
                details=report.model_dump(),
                success=False,
                error=(
                    f"crash guard triggered: {len(self._crashes)} kernel "
                    f"crashes within window of {self._window_ticks} ticks "
                    f"(threshold {self._threshold})"
                ),
            )

            if self._pause_action is not None:
                try:
                    self._pause_action(report)
                except Exception:  # noqa: BLE001 — pause failure is logged,
                    pass           # not escalated
            return report
        except Exception:  # noqa: BLE001 — the guard itself must not crash
            return None

    def rearm(self) -> None:
        """Re-arm after an explicit human resume. The sliding window is
        kept — old crashes age out naturally (窗口继续滑动)."""
        self._triggered = False
