"""Reliability mechanisms: timeout, retry, lock lease, deterministic replay.

Per SPEC §14:
- Agent execution failure: rollback, retry, block, notify
- Sub-task timeout: mark expired, notify owner
- Lock timeout: auto-release, audit, notify
- Email delivery failure: exponential backoff, max retries
- Deterministic replay: same input → same output
"""

from __future__ import annotations

import copy
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from my_team.audit import AuditEventType, AuditLog
from my_team.shared_kb import LockManager
from my_team.task_tree import TaskTree


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

    def check_task_timeouts(self, current_tick: int) -> list[str]:
        """Check for and expire overdue tasks.

        Returns list of expired task IDs.
        """
        expired = self._tasks.get_expired_tasks(current_tick)
        expired_ids: list[str] = []

        for task in expired:
            self._tasks.expire_task(task.task_id, current_tick)
            expired_ids.append(task.task_id)

            self._audit.record(
                AuditEventType.AGENT_FAILED,
                agent_id=task.owner_agent_id,
                tick=current_tick,
                details={
                    "task_id": task.task_id,
                    "failure_type": "timeout",
                    "deadline_tick": task.deadline_tick,
                    "owner": task.owner_agent_id,
                },
                success=False,
                error=f"Task expired at tick {current_tick} (deadline: {task.deadline_tick})",
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

    def check_all(self, current_tick: int) -> dict[str, Any]:
        """Run all timeout checks. Returns summary."""
        expired_tasks = self.check_task_timeouts(current_tick)
        expired_locks = self.check_lock_timeouts(current_tick)

        return {
            "tick": current_tick,
            "expired_tasks": expired_tasks,
            "expired_locks": expired_locks,
        }


class DeterministicReplay:
    """In-memory snapshot storage and deterministic conflict ordering.

    Per SPEC §2.3, §18.8. Guarantees are limited to:

    **Within scope (deterministic):**
    - In-memory state snapshots per tick
    - Conflict resolution ordering (by agent_id + effect_id)
    - Fixed action inputs producing fixed outputs

    **Outside scope (NOT guaranteed):**
    - Cross-process replay (no serialization)
    - LLM output replay (no LLM integration yet)
    - File system state consistency (external to in-memory)
    - Thread scheduling determinism (single-threaded)
    - Random number reproducibility (no seed control)
    - Network/external service responses

    To achieve full replay, the following must also be saved:
    initial state, tick duration config, mail delivery schedule,
    agent observations, action plans, tool results, random seeds,
    commit decisions.
    """

    def __init__(self) -> None:
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._action_log: dict[int, list[dict[str, Any]]] = {}

    def save_tick_state(self, tick: int, state: dict[str, Any]) -> None:
        """Save the state snapshot for a tick (deep copy)."""
        self._snapshots[tick] = copy.deepcopy(state)

    def get_tick_state(self, tick: int) -> dict[str, Any] | None:
        """Get the saved state for a tick."""
        return self._snapshots.get(tick)

    def save_tick_actions(self, tick: int, actions: list[dict[str, Any]]) -> None:
        """Save the actions taken during a tick."""
        self._action_log[tick] = list(actions)

    def get_tick_actions(self, tick: int) -> list[dict[str, Any]]:
        """Get the actions taken during a tick."""
        return list(self._action_log.get(tick, []))

    def verify_determinism(
        self,
        tick: int,
        new_state: dict[str, Any],
    ) -> bool:
        """Verify that a tick's execution is deterministic.

        Compares new_state against saved snapshot.
        Returns True if they match (deterministic).
        """
        saved = self._snapshots.get(tick)
        if saved is None:
            return True  # no previous run to compare against
        return saved == new_state

    def resolve_conflicts(
        self,
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Resolve conflicting actions deterministically.

        Sorts by agent_id then action_type for consistent ordering,
        regardless of the order actions were produced.
        """
        return sorted(
            actions,
            key=lambda a: (a.get("agent_id", ""), a.get("action_type", "")),
        )

    def is_tick_immutable(self, tick: int) -> bool:
        """Check if a tick has been finalized (saved)."""
        return tick in self._snapshots

    @property
    def finalized_ticks(self) -> list[int]:
        """List of ticks that have been finalized."""
        return sorted(self._snapshots.keys())
