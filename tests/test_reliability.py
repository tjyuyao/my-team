"""Tests for Phase 5: Reliability mechanisms.

Covers: timeout/retry, lock lease, deterministic replay.
"""

from datetime import datetime, timedelta, timezone

import pytest

from my_team.audit import AuditEventType, AuditLog
from my_team.models.task import TaskStatus
from my_team.reliability import (
    DeterministicReplay,
    FailureType,
    RetryManager,
    RetryPolicy,
    TimeoutChecker,
)
from my_team.shared_kb import LockManager
from my_team.task_tree import TaskTree

_BASE = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# RetryManager
# ---------------------------------------------------------------------------

class TestRetryManager:
    def test_record_failure(self):
        rm = RetryManager()
        record = rm.record_failure(
            "agent.a", "task.1", FailureType.TOOL_ERROR,
            "file not found", tick=5,
        )
        assert record.failure_type == FailureType.TOOL_ERROR
        assert record.retry_count == 0
        assert record.retryable is True

    def test_can_retry(self):
        rm = RetryManager(RetryPolicy(max_retries=3))
        rm.record_failure("agent.a", "task.1", FailureType.TOOL_ERROR, "err", tick=0)
        assert rm.can_retry("agent.a", "task.1")

    def test_max_retries_exceeded(self):
        rm = RetryManager(RetryPolicy(max_retries=2))
        rm.record_failure("agent.a", "task.1", FailureType.TOOL_ERROR, "err", tick=0)
        rm.execute_retry("agent.a", "task.1", tick=1)
        rm.record_failure("agent.a", "task.1", FailureType.TOOL_ERROR, "err", tick=2)
        rm.execute_retry("agent.a", "task.1", tick=3)
        rm.record_failure("agent.a", "task.1", FailureType.TOOL_ERROR, "err", tick=4)
        assert not rm.can_retry("agent.a", "task.1")

    def test_exponential_backoff(self):
        rm = RetryManager(RetryPolicy(
            base_delay_ticks=1, backoff_multiplier=2.0, max_delay_ticks=10,
        ))
        rm.record_failure("agent.a", "task.1", FailureType.TIMEOUT, "timeout", tick=0)

        # First retry: delay = 1 * 2^0 = 1
        delay = rm.execute_retry("agent.a", "task.1", tick=1)
        assert delay == 1

        rm.record_failure("agent.a", "task.1", FailureType.TIMEOUT, "timeout", tick=2)
        # Second retry: delay = 1 * 2^1 = 2
        delay = rm.execute_retry("agent.a", "task.1", tick=3)
        assert delay == 2

    def test_max_delay_cap(self):
        rm = RetryManager(RetryPolicy(
            base_delay_ticks=10, backoff_multiplier=2.0, max_delay_ticks=5,
        ))
        rm.record_failure("agent.a", "task.1", FailureType.TIMEOUT, "timeout", tick=0)
        delay = rm.execute_retry("agent.a", "task.1", tick=1)
        assert delay == 5  # capped at max_delay_ticks (10 * 2^0 = 10, capped to 5)

    def test_non_retryable_failure(self):
        rm = RetryManager()
        rm.record_failure(
            "agent.a", "task.1", FailureType.PERMISSION_DENIED,
            "access denied", tick=0, retryable=False,
        )
        assert not rm.can_retry("agent.a", "task.1")

    def test_mark_resolved(self):
        rm = RetryManager()
        rm.record_failure("agent.a", "task.1", FailureType.TOOL_ERROR, "err", tick=0)
        assert len(rm) == 1
        rm.mark_resolved("agent.a", "task.1")
        assert len(rm) == 0

    def test_failure_audit_logged(self):
        log = AuditLog()
        rm = RetryManager(audit_log=log)
        rm.record_failure("agent.a", "task.1", FailureType.TOOL_ERROR, "err", tick=5)
        entries = log.for_event_type(AuditEventType.AGENT_FAILED)
        assert len(entries) == 1
        assert entries[0].agent_id == "agent.a"
        assert entries[0].tick == 5

    def test_retry_audit_logged(self):
        log = AuditLog()
        rm = RetryManager(audit_log=log)
        rm.record_failure("agent.a", "task.1", FailureType.TOOL_ERROR, "err", tick=0)
        rm.execute_retry("agent.a", "task.1", tick=1)
        entries = log.for_event_type(AuditEventType.AGENT_RETRY)
        assert len(entries) == 1
        assert entries[0].details["retry_number"] == 1


# ---------------------------------------------------------------------------
# TimeoutChecker
# ---------------------------------------------------------------------------

class TestTimeoutChecker:
    @pytest.fixture
    def checker(self):
        task_tree = TaskTree()
        lock_manager = LockManager()
        audit_log = AuditLog()
        checker = TimeoutChecker(task_tree, lock_manager, audit_log)
        return checker, task_tree, lock_manager, audit_log

    def test_check_task_timeouts(self, checker):
        tc, task_tree, _, _ = checker
        task_tree.create(
            task_id="t1", title="Task",
            assigner_agent_id="a", assignee_agent_id="a",
            deadline=_BASE + timedelta(minutes=5),
        )
        task_tree.update_status("t1", TaskStatus.ASSIGNED, tick=0)
        task_tree.update_status("t1", TaskStatus.ACCEPTED, tick=1)
        task_tree.update_status("t1", TaskStatus.IN_PROGRESS, tick=2)

        expired = tc.check_task_timeouts(now=_BASE + timedelta(minutes=7), tick=7)
        assert "t1" in expired

    def test_check_lock_timeouts(self, checker):
        tc, _, lock_manager, _ = checker
        lock_manager.acquire("resource/a", "agent.a", current_tick=0, lease_ticks=2)
        # Lease expires at tick 2
        released = tc.check_lock_timeouts(current_tick=3)
        assert len(released) == 1
        assert released[0]["resource"] == "resource/a"

    def test_check_all(self, checker):
        tc, task_tree, lock_manager, _ = checker
        task_tree.create(
            task_id="t1", title="Task",
            assigner_agent_id="a", assignee_agent_id="a",
            deadline=_BASE + timedelta(minutes=5),
        )
        task_tree.update_status("t1", TaskStatus.ASSIGNED, tick=0)
        task_tree.update_status("t1", TaskStatus.ACCEPTED, tick=1)
        task_tree.update_status("t1", TaskStatus.IN_PROGRESS, tick=2)

        lock_manager.acquire("res", "agent.a", current_tick=0, lease_ticks=2)

        result = tc.check_all(now=_BASE + timedelta(minutes=7), tick=7)
        assert len(result["expired_tasks"]) == 1
        assert len(result["expired_locks"]) == 1

    def test_no_timeouts(self, checker):
        tc, task_tree, lock_manager, _ = checker
        task_tree.create(
            task_id="t1", title="Task",
            assigner_agent_id="a", assignee_agent_id="a",
            deadline=_BASE + timedelta(minutes=100),
        )
        task_tree.update_status("t1", TaskStatus.ASSIGNED, tick=0)

        lock_manager.acquire("res", "agent.a", current_tick=0, lease_ticks=10)

        result = tc.check_all(now=_BASE + timedelta(minutes=5), tick=5)
        assert len(result["expired_tasks"]) == 0
        assert len(result["expired_locks"]) == 0

    def test_timeout_audit_logged(self, checker):
        tc, task_tree, _, audit_log = checker
        task_tree.create(
            task_id="t1", title="Task",
            assigner_agent_id="a", assignee_agent_id="a",
            deadline=_BASE + timedelta(minutes=5),
        )
        task_tree.update_status("t1", TaskStatus.ASSIGNED, tick=0)
        task_tree.update_status("t1", TaskStatus.ACCEPTED, tick=1)
        task_tree.update_status("t1", TaskStatus.IN_PROGRESS, tick=2)

        tc.check_task_timeouts(now=_BASE + timedelta(minutes=7), tick=7)
        entries = audit_log.for_event_type(AuditEventType.AGENT_FAILED)
        assert len(entries) == 1
        assert entries[0].details["failure_type"] == "timeout"


# ---------------------------------------------------------------------------
# DeterministicReplay
# ---------------------------------------------------------------------------

class TestDeterministicReplay:
    def test_save_and_get_state(self):
        dr = DeterministicReplay()
        dr.save_tick_state(0, {"agents": {"a": "idle"}})
        state = dr.get_tick_state(0)
        assert state == {"agents": {"a": "idle"}}

    def test_save_and_get_actions(self):
        dr = DeterministicReplay()
        actions = [{"agent_id": "a", "action_type": "write"}]
        dr.save_tick_actions(0, actions)
        assert dr.get_tick_actions(0) == actions

    def test_verify_determinism_match(self):
        dr = DeterministicReplay()
        state = {"tick": 0, "data": "x"}
        dr.save_tick_state(0, state)
        assert dr.verify_determinism(0, {"tick": 0, "data": "x"})

    def test_verify_determinism_mismatch(self):
        dr = DeterministicReplay()
        dr.save_tick_state(0, {"tick": 0, "data": "x"})
        assert not dr.verify_determinism(0, {"tick": 0, "data": "y"})

    def test_verify_determinism_no_previous(self):
        dr = DeterministicReplay()
        assert dr.verify_determinism(999, {"anything": True})

    def test_resolve_conflicts_deterministic(self):
        dr = DeterministicReplay()
        actions = [
            {"agent_id": "agent.b", "action_type": "write"},
            {"agent_id": "agent.a", "action_type": "read"},
            {"agent_id": "agent.a", "action_type": "write"},
        ]
        resolved = dr.resolve_conflicts(actions)
        assert resolved[0]["agent_id"] == "agent.a"
        assert resolved[0]["action_type"] == "read"
        assert resolved[1]["agent_id"] == "agent.a"
        assert resolved[1]["action_type"] == "write"
        assert resolved[2]["agent_id"] == "agent.b"

    def test_is_tick_immutable(self):
        dr = DeterministicReplay()
        assert not dr.is_tick_immutable(0)
        dr.save_tick_state(0, {"data": True})
        assert dr.is_tick_immutable(0)

    def test_finalized_ticks(self):
        dr = DeterministicReplay()
        dr.save_tick_state(2, {})
        dr.save_tick_state(0, {})
        dr.save_tick_state(1, {})
        assert dr.finalized_ticks == [0, 1, 2]

    def test_state_isolation(self):
        """Saved state should be a copy, not a reference."""
        dr = DeterministicReplay()
        original = {"data": [1, 2, 3]}
        dr.save_tick_state(0, original)
        original["data"].append(4)
        assert dr.get_tick_state(0)["data"] == [1, 2, 3]
