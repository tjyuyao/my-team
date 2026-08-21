"""Tests for task cancellation cascade and root agent isolation.

Covers review gaps:
- Task cancellation propagation (parent → child cascade)
- Root agent capability restrictions
"""

from datetime import datetime, timedelta, timezone

import pytest

from my_team.agent_runtime import (
    ROOT_TOOLS,
    RootAgent,
    ToolPermissionError,
    ToolRegistry,
)
from my_team.models.task import TaskStatus
from my_team.task_tree import TaskTree

# ---------------------------------------------------------------------------
# Task Cancellation Cascade
# ---------------------------------------------------------------------------

class TestTaskCancellation:
    @pytest.fixture
    def tree_with_hierarchy(self):
        """Task tree with parent → child → grandchild."""
        tt = TaskTree()
        parent = tt.create(
            task_id="task.parent",
            title="Parent",
            creator_agent_id="agent.root",
            owner_agent_id="agent.research",
            deadline=_BASE + timedelta(minutes=20),
        )
        child = tt.create(
            task_id="task.child",
            title="Child",
            creator_agent_id="agent.research",
            owner_agent_id="agent.web_research",
            parent_task_id=parent.task_id,
            deadline=_BASE + timedelta(minutes=15),
        )
        grandchild = tt.create(
            task_id="task.grandchild",
            title="Grandchild",
            creator_agent_id="agent.web_research",
            owner_agent_id="agent.web_research",
            parent_task_id=child.task_id,
            deadline=_BASE + timedelta(minutes=10),
        )
        return tt, parent, child, grandchild

    def test_cancel_parent_cascades_to_children(self, tree_with_hierarchy):
        tt, parent, child, grandchild = tree_with_hierarchy
        cancelled = tt.cancel_task(parent.task_id, tick=5)
        assert len(cancelled) == 3
        assert parent.status == TaskStatus.CANCELLED
        assert child.status == TaskStatus.CANCELLED
        assert grandchild.status == TaskStatus.CANCELLED

    def test_cancel_skips_completed_children(self, tree_with_hierarchy):
        tt, parent, child, grandchild = tree_with_hierarchy
        # Complete the child first (ASSIGNED → ACCEPTED → IN_PROGRESS → SUBMITTED → COMPLETED)
        child.transition_to(TaskStatus.ASSIGNED, tick=1)
        child.transition_to(TaskStatus.ACCEPTED, tick=2)
        child.transition_to(TaskStatus.IN_PROGRESS, tick=3)
        child.transition_to(TaskStatus.SUBMITTED, tick=4)
        child.transition_to(TaskStatus.COMPLETED, tick=4)

        cancelled = tt.cancel_task(parent.task_id, tick=5)
        # Parent and grandchild cancelled, child already completed
        assert parent.status == TaskStatus.CANCELLED
        assert child.status == TaskStatus.COMPLETED  # not affected
        assert grandchild.status == TaskStatus.CANCELLED
        assert len(cancelled) == 2  # parent + grandchild

    def test_cancel_already_cancelled_noop(self, tree_with_hierarchy):
        tt, parent, child, grandchild = tree_with_hierarchy
        tt.cancel_task(parent.task_id, tick=1)
        # Cancel again — should not fail, already terminal
        cancelled = tt.cancel_task(parent.task_id, tick=2)
        assert len(cancelled) == 0  # all already terminal

    def test_cancel_nonexistent_task(self):
        tt = TaskTree()
        cancelled = tt.cancel_task("nonexistent", tick=0)
        assert len(cancelled) == 0

    def test_cancel_leaf_task(self):
        tt = TaskTree()
        task = tt.create(
            task_id="task.solo",
            title="Solo",
            creator_agent_id="agent.root",
            owner_agent_id="agent.root",
        )
        cancelled = tt.cancel_task(task.task_id, tick=0)
        assert len(cancelled) == 1
        assert task.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# Root Agent Capability Restrictions
# ---------------------------------------------------------------------------

class TestRootAgentIsolation:
    @pytest.fixture
    def root_agent(self):
        tr = ToolRegistry()
        tr.register_agent("agent.root", ROOT_TOOLS)
        return RootAgent(agent_id="agent.root", tool_registry=tr)

    def test_root_cannot_send_email(self, root_agent):
        ctx = root_agent.tool_context
        assert "send_email" not in ctx.allowed_tools
        with pytest.raises(ToolPermissionError):
            root_agent._tool_registry.authorize(ctx, "send_email")

    def test_root_cannot_use_web_search(self, root_agent):
        ctx = root_agent.tool_context
        assert "web_search" not in ctx.allowed_tools
        with pytest.raises(ToolPermissionError):
            root_agent._tool_registry.authorize(ctx, "web_search")

    def test_root_can_read(self, root_agent):
        ctx = root_agent.tool_context
        assert "read" in ctx.allowed_tools

    def test_root_can_write(self, root_agent):
        ctx = root_agent.tool_context
        assert "write" in ctx.allowed_tools

    def test_root_can_delegate(self, root_agent):
        ctx = root_agent.tool_context
        assert "delegate" in ctx.allowed_tools

    def test_root_cannot_directly_write_shared_kb(self, root_agent):
        """Root has no 'write_shared' tool — can only write via delegate."""
        ctx = root_agent.tool_context
        assert "write_shared" not in ctx.allowed_tools

    def test_root_cannot_directly_commit_effects(self, root_agent):
        """Root cannot directly commit transaction effects."""
        ctx = root_agent.tool_context
        assert "commit_effect" not in ctx.allowed_tools


_BASE = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
