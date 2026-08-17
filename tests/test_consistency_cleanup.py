"""Tests for T8: consistency and observability cleanup.

Date: 2026-08-18
"""

from __future__ import annotations

from pathlib import Path

from my_team.agent_tree import AgentTree
from my_team.models.activation import ReadyCandidate, WakeCondition, WakeEventType, WakeupEvent
from my_team.scheduler import AgentScheduler, _matches
from my_team.simulation import Simulation


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
                "can_delegate": False,
                "metadata": {"bootstrap": True},
            },
        ],
    })


class TestVersionConsistency:
    """__version__ must match pyproject.toml version."""

    def test_version_matches_pyproject(self) -> None:
        from tomllib import loads

        import my_team
        pyproject = Path("pyproject.toml").read_text()
        data = loads(pyproject)
        assert my_team.__version__ == data["project"]["version"]


class TestEventVisibility:
    """WakeupEvent.visible_at_tick controls when events match."""

    def test_event_not_visible_before_visible_at_tick(self) -> None:
        cond = WakeCondition(
            event_types={WakeEventType.BOOTSTRAP},
            wake_at_tick=0,
        )
        event = WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.root",
            tick=0,
            visible_at_tick=1,
        )
        # At tick 0, event with visible_at_tick=1 should NOT match
        assert not _matches(cond, event, tick=0)

    def test_event_visible_at_visible_at_tick(self) -> None:
        cond = WakeCondition(
            event_types={WakeEventType.BOOTSTRAP},
            wake_at_tick=0,
        )
        event = WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.root",
            tick=0,
            visible_at_tick=1,
        )
        # At tick 1, event should match
        assert _matches(cond, event, tick=1)

    def test_event_with_visible_at_tick_zero_matches_immediately(self) -> None:
        cond = WakeCondition(
            event_types={WakeEventType.BOOTSTRAP},
            wake_at_tick=0,
        )
        event = WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.root",
            tick=0,
            visible_at_tick=0,
        )
        assert _matches(cond, event, tick=0)

    def test_enqueue_sets_visible_at_tick(self) -> None:
        sched = AgentScheduler()
        event = WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.root",
            tick=5,
        )
        sched.enqueue_event(event)
        # visible_at_tick should be set to tick+1 = 6
        assert event.visible_at_tick == 6

    def test_enqueue_preserves_explicit_visible_at_tick(self) -> None:
        sched = AgentScheduler()
        event = WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.root",
            tick=5,
            visible_at_tick=5,  # explicit immediate
        )
        sched.enqueue_event(event)
        assert event.visible_at_tick == 5


class TestWorkspaceVersionBinding:
    """Queued ops use submission-tick workspace view, not current tick."""

    def test_queued_op_binds_submission_view(self) -> None:
        """Op submitted at tick 0 gets tick-0 snapshot in metadata."""
        sim = Simulation(agent_tree=_make_tree())
        from tests.tool_helpers import register_remote_tool
        register_remote_tool(sim, "remote_calc")
        from my_team.models.intent import SubmitToolRequest
        intent = SubmitToolRequest(
            agent_id="agent.root",
            tool_name="remote_calc",
            arguments={"x": 1},
            timeout_ticks=10,
        )
        plan = {"agent.root": [intent]}
        from my_team.agent_runtime import ActionResult, AgentAction
        validated = {"agent.root": [ActionResult(
            action=AgentAction(
                action_type="submit_tool_request",
                tool_name="remote_calc",
                payload=dict(intent.payload),
            ),
            success=True,
            result_data={"validated": True},
        )]}
        snapshot = sim._build_snapshot(0)
        sim._phase_act(
            0, plan,
            ready=[ReadyCandidate(agent_id="agent.root", events=(), tick=0)],
            validated=validated,
            snapshot=snapshot,
        )
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1
        # Submission view should be bound
        assert "_submission_view" in ops[0].metadata
