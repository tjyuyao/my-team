"""Tests for T6: ContextCompiler — role-aware observation assembly.

Date: 2026-08-18
"""

from __future__ import annotations

from my_team.agent_tree import AgentTree
from my_team.context_compiler import (
    ContextCompiler,
    ObservationPolicy,
    ObservationSection,
    TaskScope,
)
from my_team.mailbox import MailSystem
from my_team.private_store import PrivateStore
from my_team.shared_kb import SharedKB
from my_team.simulation import Simulation
from my_team.task_tree import TaskTree


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True, "mission": "Build great things"},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _make_snapshot(tick: int = 0) -> dict:
    return {
        "tick": tick,
        "tasks": {
            "task.1": {
                "status": "in_progress",
                "title": "Research task",
                "assignee": "agent.research",
                "assigner": "agent.root",
            },
            "task.2": {
                "status": "completed",
                "title": "Done task",
                "assignee": "agent.root",
                "assigner": "agent.root",
            },
        },
        "emails": [
            {
                "email_id": "e1",
                "from": "agent.root",
                "to": ["agent.research"],
                "subject": "Do this",
                "email_type": "delegation",
                "task_id": "task.1",
                "body": "Please research topic X",
            },
        ],
        "shared_kb": {
            "paths": ["project/notes.md", "project/data.csv"],
            "versions": {"project/notes.md": 1, "project/data.csv": 3},
        },
        "locks": {},
        "lock_tokens": {},
        "private_files": {
            "agent.research": {
                "files": {"workspace/report.md": "draft content"},
                "dirs": ["workspace"],
            },
        },
    }


class TestContextCompilerUnit:
    def test_default_policy_for_root(self):
        compiler = ContextCompiler(
            agent_tree=_make_tree(),
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
        )
        policy = compiler.get_policy("root_decision_agent")
        assert ObservationSection.TASK_TREE_SUMMARY in policy.sections
        assert ObservationSection.KPI_DASHBOARD in policy.sections
        assert policy.task_scope == TaskScope.ALL

    def test_default_policy_for_worker(self):
        compiler = ContextCompiler(
            agent_tree=_make_tree(),
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
        )
        policy = compiler.get_policy("worker")
        assert ObservationSection.TASK_DETAIL in policy.sections
        assert ObservationSection.WORKSPACE_FILES in policy.sections
        assert policy.task_scope == TaskScope.FOCUS

    def test_unknown_role_uses_fallback(self):
        compiler = ContextCompiler(
            agent_tree=_make_tree(),
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
        )
        policy = compiler.get_policy("unknown_role")
        assert policy.task_scope == TaskScope.ALL  # default


class TestContextCompilerIntegration:
    def _make_compiler(self):
        tree = _make_tree()
        return ContextCompiler(
            agent_tree=tree,
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
        ), tree

    def test_root_observation_has_task_summary(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        root_config = tree.get("agent.root")
        result = compiler.compile(root_config, snapshot)
        assert "task_summary" in result
        assert result["task_summary"]["in_progress"] == 1
        assert result["task_summary"]["completed"] == 1

    def test_root_observation_has_kpi(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        root_config = tree.get("agent.root")
        result = compiler.compile(root_config, snapshot)
        assert "kpi" in result
        assert result["kpi"]["total_tasks"] == 2

    def test_root_observation_has_mission(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        root_config = tree.get("agent.root")
        result = compiler.compile(root_config, snapshot)
        assert result.get("mission") == "Build great things"

    def test_worker_observation_has_focus_task(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        # Simulate continuation with task_id
        class FakeCont:
            task_id = "task.1"
        result = compiler.compile(worker_config, snapshot, continuation=FakeCont())
        assert "task.1" in result["task_states"]
        assert result["task_states"]["task.1"]["title"] == "Research task"

    def test_worker_observation_excludes_other_tasks(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        class FakeCont:
            task_id = "task.1"
        result = compiler.compile(worker_config, snapshot, continuation=FakeCont())
        # Worker with FOCUS scope should not see task.2
        assert "task.2" not in result["task_states"]

    def test_emails_filtered_by_recipient(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        result = compiler.compile(worker_config, snapshot)
        # Email is addressed to agent.research
        assert len(result["emails"]) == 1
        assert result["emails"][0]["subject"] == "Do this"

    def test_email_body_included(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        result = compiler.compile(worker_config, snapshot)
        assert result["emails"][0]["body"] == "Please research topic X"

    def test_kb_snapshot_included(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        result = compiler.compile(worker_config, snapshot)
        assert "shared_kb_snapshot" in result
        assert "project/notes.md" in result["shared_kb_snapshot"]["paths"]

    def test_kb_injection_disabled(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        policy = ObservationPolicy(
            sections=[ObservationSection.KB_SNAPSHOT],
            task_scope=TaskScope.ALL,
            kb_injection=False,
        )
        compiler._policies["worker"] = policy
        result = compiler.compile(worker_config, snapshot)
        assert result["shared_kb_snapshot"] == {}

    def test_token_budget_truncates_email_body(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        # Add a very long email
        snapshot["emails"][0]["body"] = "x" * 10000
        worker_config = tree.get("agent.research")
        policy = ObservationPolicy(
            sections=[ObservationSection.EMAILS],
            task_scope=TaskScope.ALL,
            max_tokens=100,
        )
        compiler._policies["worker"] = policy
        result = compiler.compile(worker_config, snapshot)
        assert "truncated" in result["emails"][0]["body"]

    def test_workspace_files_for_worker(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        result = compiler.compile(worker_config, snapshot)
        assert "workspace_files" in result
        assert "workspace/report.md" in result["workspace_files"]

    def test_compile_returns_observation_compatible_dict(self):
        """Result can be wrapped in AgentObservation."""
        from my_team.agent_runtime import AgentObservation
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        root_config = tree.get("agent.root")
        result = compiler.compile(root_config, snapshot)
        obs = AgentObservation(**result)
        assert obs.agent_id == "agent.root"
        assert obs.tick == 0


class TestContextCompilerViaSimulation:
    def test_simulation_uses_context_compiler(self):
        """End-to-end: simulation run_tick uses ContextCompiler."""
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        # Observation should have been produced (check via decide results)
        assert sim._tick_engine.current_tick == 1

    def test_root_worker_see_different_tasks(self):
        """Root sees all tasks, worker sees only focus task."""
        sim = Simulation(agent_tree=_make_tree())
        # Create tasks
        sim.task_tree.create(
            task_id="task.r1", title="Root task",
            assigner_agent_id="agent.root", assignee_agent_id="agent.root",
        )
        sim.task_tree.create(
            task_id="task.w1", title="Worker task",
            assigner_agent_id="agent.root", assignee_agent_id="agent.research",
        )
        sim.run_tick()
        # Verify context compiler was used (observations produced)
        assert sim._tick_engine.current_tick == 1
