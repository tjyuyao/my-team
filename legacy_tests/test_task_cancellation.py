"""Tests for task cancellation cascade and root agent isolation.

Covers review gaps:
- Task cancellation propagation (parent → child cascade)
- Root agent capability restrictions

v0.11（N1b，§5.1）：root 能力边界断言迁移为两层 Grant 求值——测试内
显式布线：建 Authority → 注册工具 uuid → grant_membership +
grant_capability（§3.5）；未注册 uuid / 未授予工具一律拒绝
（deny-by-default）。
"""

from datetime import datetime, timedelta, timezone

import pytest

from my_team.agent_runtime import (
    RootAgent,
    ToolPermissionError,
    ToolRegistry,
)
from my_team.devices.authority import Authority, new_team_id
from my_team.models.task import TaskStatus
from my_team.task_tree import TaskTree
from my_team.tool_manifest import builtin_manifests

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
            assigner_agent_id="agent.root",
            assignee_agent_id="agent.research",
            deadline=_BASE + timedelta(minutes=20),
        )
        child = tt.create(
            task_id="task.child",
            title="Child",
            assigner_agent_id="agent.research",
            assignee_agent_id="agent.web_research",
            derived_from=parent.task_id,
            deadline=_BASE + timedelta(minutes=15),
        )
        grandchild = tt.create(
            task_id="task.grandchild",
            title="Grandchild",
            assigner_agent_id="agent.web_research",
            assignee_agent_id="agent.web_research",
            derived_from=child.task_id,
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
            assigner_agent_id="agent.root",
            assignee_agent_id="agent.root",
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
        """N1b 两层 Grant 布线：root 以自身为 position，仅授予
        read/write/ls/delegate（直派形态，§3.5/§5.1）。"""
        authority = Authority(team_id=new_team_id(), owner_agent_id="agent.root")
        reg = ToolRegistry(authority=authority)
        for manifest in builtin_manifests().values():
            reg.register_manifest(manifest)
        reg.declare_tools(
            "agent.root",
            frozenset({"read", "write", "ls", "delegate"}),
        )
        return RootAgent(agent_id="agent.root", tool_registry=reg)

    def test_root_cannot_send_email(self, root_agent):
        ctx = root_agent.tool_context
        assert not root_agent._tool_registry.can_use("agent.root", "send_email")
        with pytest.raises(ToolPermissionError):
            root_agent._tool_registry.authorize(ctx, "send_email")

    def test_root_cannot_use_web_search(self, root_agent):
        ctx = root_agent.tool_context
        # web_search 无 manifest（未注册 uuid）→ deny-by-default（§3.5）
        assert not root_agent._tool_registry.can_use("agent.root", "web_search")
        with pytest.raises(ToolPermissionError):
            root_agent._tool_registry.authorize(ctx, "web_search")

    def test_root_can_read(self, root_agent):
        assert root_agent._tool_registry.can_use("agent.root", "read")

    def test_root_can_write(self, root_agent):
        assert root_agent._tool_registry.can_use("agent.root", "write")

    def test_root_can_delegate(self, root_agent):
        assert root_agent._tool_registry.can_use("agent.root", "delegate")

    def test_root_cannot_directly_write_shared_kb(self, root_agent):
        """Root 未获 kb 写能力（'write_shared' 无 manifest/未授予）——
        只能经 delegate 间接写（§3.5 两层 Grant）。"""
        assert not root_agent._tool_registry.can_use("agent.root", "write_shared")

    def test_root_cannot_directly_commit_effects(self, root_agent):
        """'commit_effect' 无 manifest/未授予 → 拒绝（deny-by-default）。"""
        assert not root_agent._tool_registry.can_use("agent.root", "commit_effect")


_BASE = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
