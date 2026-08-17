"""Tests for snapshot consistency, commit atomicity, and cross-effect correctness.

These tests verify the correctness properties identified in the v0.5.0 review:
- read/ls see previous tick's state (not same-tick writes)
- Task+Email consistency across delegate actions
- Commit failure behavior
"""

from __future__ import annotations

from my_team.agent_runtime import (
    ActionPlan,
    AgentAction,
    AgentObservation,
    BaseAgent,
)
from my_team.agent_tree import AgentTree
from my_team.simulation import Simulation


class ScriptedAgent(BaseAgent):
    """Agent with deterministic scripted behavior."""

    def __init__(
        self,
        agent_id: str,
        script: dict[int, ActionPlan] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self._script = script or {}

    def decide(self, observation: AgentObservation) -> ActionPlan:
        return self._script.get(observation.tick, ActionPlan(
            agent_id=self._agent_id,
            tick=observation.tick,
            actions=[],
        ))


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
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


class TestSnapshotConsistency:
    """Verify that reads see previous tick's committed state, not same-tick writes."""

    def test_write_not_visible_until_next_tick(self) -> None:
        """A file written in tick 0 should not be readable in tick 0,
        but should be readable in tick 1."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        # Tick 0: root writes a file
        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="write",
                        tool_name="write",
                        payload={"path": "data.md", "content": "v1"},
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root
        sim.run_tick()

        # After tick 0: file should exist (committed in Phase 8)
        home = sim._private_store.agent_home("agent.root")
        assert (home / "data.md").exists()
        assert (home / "data.md").read_text() == "v1"

    def test_read_sees_previous_tick_state(self) -> None:
        """If a file exists before tick 0, root should see it in tick 0."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        # Pre-create a file
        home = sim._private_store.agent_home("agent.root")
        home.mkdir(parents=True, exist_ok=True)
        (home / "existing.md").write_text("original")

        # Tick 0: root reads the file
        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="read",
                        tool_name="read",
                        payload={"path": "existing.md"},
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root
        sim.run_tick()

        # The read should have succeeded (file exists with original content)
        assert (home / "existing.md").read_text() == "original"

    def test_write_staged_not_applied_during_act(self) -> None:
        """A staged write should not modify the filesystem until commit."""
        from uuid import uuid4

        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        home = sim._private_store.agent_home("agent.root")
        home.mkdir(parents=True, exist_ok=True)

        filename = f"staged_{uuid4().hex[:8]}.md"
        filepath = home / filename

        # Verify file doesn't exist before tick
        assert not filepath.exists()

        # Tick 0: root writes
        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="write",
                        tool_name="write",
                        payload={"path": filename, "content": "staged"},
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        # Manually step through phases to check mid-tick state
        tick = sim._tick_engine.current_tick
        snapshot = sim._build_snapshot(tick)
        delivered = sim._phase_deliver(tick)  # noqa: F841
        ready = sim._phase_schedule(tick)
        observations = sim._phase_observe(tick, snapshot, ready)
        plans = sim._phase_decide(tick, observations, ready)
        validated = sim._phase_validate(tick, plans, ready)

        # After Act: file should NOT exist yet (staged, not applied)
        sim._phase_act(tick, plans, ready, validated)
        # Buffer has staged effect, but file not yet written
        assert sim._transaction_buffer.has_pending

        # After commit: file exists
        sim._phase_commit(tick, {})
        assert filepath.exists()
        assert filepath.read_text() == "staged"


class TestTaskEmailConsistency:
    """Verify that delegate creates both task and email consistently."""

    def test_delegate_creates_task_and_email(self) -> None:
        """A delegate action should create both a task and an email."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Consistency Test",
                            "task_description": "Verify task+email",
                        },
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root
        sim.run_tick()

        # Both task and email should exist after commit
        assert len(sim.task_tree.get_active_tasks()) == 1
        assert len(sim._mail_system._all_emails) == 1

        task = sim.task_tree.get_active_tasks()[0]
        assert task.title == "Consistency Test"
        assert task.owner_agent_id == "agent.research"

    def test_task_created_with_assigned_status(self) -> None:
        """Tasks created by delegate should have ASSIGNED status (not DRAFT)."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Status Test",
                            "task_description": "Check status",
                        },
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root
        sim.run_tick()

        tasks = sim.task_tree.get_active_tasks()
        assert len(tasks) == 1
        assert tasks[0].status.value == "assigned"

    def test_email_deliver_at_tick_correct(self) -> None:
        """Email should be delivered at tick + latency, not immediately."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Timing Test",
                            "task_description": "Check timing",
                        },
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root
        sim.run_tick()

        emails = list(sim._mail_system._all_emails.values())
        assert len(emails) == 1
        assert emails[0].deliver_at_tick == 1  # tick 0 + latency 1

    def test_transaction_buffer_has_both_effects(self) -> None:
        """Delegate should stage exactly 2 effects: TASK_CREATE + EMAIL_SEND."""
        from my_team.transaction import EffectType

        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Buffer Test",
                            "task_description": "Check buffer",
                        },
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        # Step through phases manually
        tick = sim._tick_engine.current_tick
        snapshot = sim._build_snapshot(tick)
        delivered = sim._phase_deliver(tick)  # noqa: F841
        ready = sim._phase_schedule(tick)
        observations = sim._phase_observe(tick, snapshot, ready)
        plans = sim._phase_decide(tick, observations, ready)
        validated = sim._phase_validate(tick, plans, ready)
        sim._phase_act(tick, plans, ready, validated)

        # Check staged effects
        effects = sim._transaction_buffer.get_effects()
        effect_types = {e.effect_type for e in effects}
        assert EffectType.TASK_CREATE in effect_types
        assert EffectType.EMAIL_SEND in effect_types
        assert len(effects) == 2


class TestCommitBehavior:
    """Verify commit pipeline behavior."""

    def test_buffer_cleared_after_commit(self) -> None:
        """Transaction buffer should be empty after tick completes."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Cleanup Test",
                            "task_description": "Check cleanup",
                        },
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root
        sim.run_tick()

        assert not sim._transaction_buffer.has_pending
        assert sim._transaction_buffer.committed_count == 0

    def test_audit_records_transaction_commit(self) -> None:
        """Commit should record TRANSACTION_COMMIT audit events."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Audit Test",
                            "task_description": "Check audit",
                        },
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root
        sim.run_tick()

        from my_team.audit import AuditEventType
        commit_events = sim.audit_log.for_event_type(
            AuditEventType.TRANSACTION_COMMIT,
        )
        assert len(commit_events) >= 2  # TASK_CREATE + EMAIL_SEND

    def test_multiple_delegates_create_multiple_tasks(self) -> None:
        """Multiple delegate actions should create multiple tasks."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Task A",
                            "task_description": "First task",
                        },
                    ),
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Task B",
                            "task_description": "Second task",
                        },
                    ),
                ],
            ),
        }
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root
        sim.run_tick()

        tasks = sim.task_tree.get_active_tasks()
        assert len(tasks) == 2
        titles = {t.title for t in tasks}
        assert titles == {"Task A", "Task B"}
