"""Tests for PendingOperationRegistry — async external operation tracking.

Verifies the lifecycle of pending operations:
  SUBMITTED → PENDING → COMPLETED / FAILED / CANCELLED / TIMED_OUT
"""

from __future__ import annotations

from my_team.pending_ops import OpStatus, OpType, PendingOperationRegistry


class TestPendingOperationRegistry:
    """Test the pending operation registry lifecycle."""

    def test_submit_operation(self) -> None:
        """Submit creates a new operation with SUBMITTED status."""
        reg = PendingOperationRegistry()
        op = reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
            eligible_tick=8,
            task_id="task.001",
        )
        assert op.status == OpStatus.SUBMITTED
        assert op.agent_id == "agent.research"
        assert op.created_tick == 5
        assert op.eligible_tick == 8
        assert op.task_id == "task.001"

    def test_complete_operation(self) -> None:
        """Complete sets status to COMPLETED with result data."""
        reg = PendingOperationRegistry()
        op = reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        result = reg.complete(op.request_id, result={"content": "response"})
        assert result is not None
        assert result.status == OpStatus.COMPLETED
        assert result.result == {"content": "response"}

    def test_fail_operation(self) -> None:
        """Fail sets status to FAILED with error message."""
        reg = PendingOperationRegistry()
        op = reg.submit(
            op_type=OpType.TOOL_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        result = reg.fail(op.request_id, error="File not found")
        assert result is not None
        assert result.status == OpStatus.FAILED
        assert result.error == "File not found"

    def test_cancel_operation(self) -> None:
        """Cancel sets status to CANCELLED."""
        reg = PendingOperationRegistry()
        op = reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        result = reg.cancel(op.request_id)
        assert result is not None
        assert result.status == OpStatus.CANCELLED

    def test_timeout_expired(self) -> None:
        """Operations past deadline are marked TIMED_OUT."""
        reg = PendingOperationRegistry()
        op = reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
            deadline_tick=10,
        )
        # Mark as pending (dispatched)
        op.status = OpStatus.PENDING

        # Tick 11: past deadline
        expired = reg.timeout_expired(11)
        assert len(expired) == 1
        assert expired[0].status == OpStatus.TIMED_OUT

    def test_collect_completed(self) -> None:
        """Completed operations are collected when eligible_tick <= current_tick."""
        reg = PendingOperationRegistry()
        op = reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
            eligible_tick=8,
        )
        reg.complete(op.request_id, result={"content": "response"})

        # Tick 7: not yet eligible
        collected = reg.collect_completed(7)
        assert len(collected) == 0

        # Tick 8: eligible
        collected = reg.collect_completed(8)
        assert len(collected) == 1
        assert collected[0].request_id == op.request_id

    def test_get_by_agent(self) -> None:
        """Get operations filtered by agent_id."""
        reg = PendingOperationRegistry()
        reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        reg.submit(
            op_type=OpType.TOOL_REQUEST,
            agent_id="agent.root",
            created_tick=5,
        )
        research_ops = reg.get_by_agent("agent.research")
        assert len(research_ops) == 1
        assert research_ops[0].agent_id == "agent.research"

    def test_get_by_agent_with_status(self) -> None:
        """Get operations filtered by agent_id and status."""
        reg = PendingOperationRegistry()
        op1 = reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        op2 = reg.submit(
            op_type=OpType.TOOL_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        reg.complete(op1.request_id)
        op2.status = OpStatus.PENDING

        completed = reg.get_by_agent("agent.research", status=OpStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].request_id == op1.request_id

    def test_remove_completed(self) -> None:
        """Remove completed/failed/cancelled/timed_out operations."""
        reg = PendingOperationRegistry()
        op1 = reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        op2 = reg.submit(
            op_type=OpType.TOOL_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        reg.complete(op1.request_id)
        reg.fail(op2.request_id, error="failed")

        removed = reg.remove_completed()
        assert removed == 2
        assert reg.pending_count == 0

    def test_pending_count(self) -> None:
        """pending_count tracks SUBMITTED + PENDING operations."""
        reg = PendingOperationRegistry()
        op1 = reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        op2 = reg.submit(
            op_type=OpType.TOOL_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        op2.status = OpStatus.PENDING
        reg.complete(op1.request_id)

        assert reg.pending_count == 1  # only op2 is PENDING
        assert reg.completed_count == 1  # only op1 is COMPLETED

    def test_summary(self) -> None:
        """Summary returns status distribution."""
        reg = PendingOperationRegistry()
        reg.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        reg.submit(
            op_type=OpType.TOOL_REQUEST,
            agent_id="agent.research",
            created_tick=5,
        )
        summary = reg.summary()
        assert summary["total"] == 2
        assert summary["by_status"]["submitted"] == 2

    def test_nonexistent_operation(self) -> None:
        """Operations that don't exist return None."""
        reg = PendingOperationRegistry()
        assert reg.complete("nonexistent") is None
        assert reg.fail("nonexistent", error="x") is None
        assert reg.cancel("nonexistent") is None
        assert reg.get_by_id("nonexistent") is None
