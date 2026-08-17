"""Main-path tests for P0-3: TickResult truth + TickEngine clock-only.

Verifies that run_tick() returns a TickResult reflecting the real
10-phase kernel: phases_completed, committed, errors.
TickEngine no longer has phase handlers.

Date: 2026-08-18
"""

from __future__ import annotations

from my_team.agent_tree import AgentTree
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


class TestTickResultTruth:
    """run_tick() returns a TickResult matching the real kernel."""

    def test_successful_tick_returns_committed(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        result = sim.run_tick()
        assert result.committed is True
        assert result.errors == []
        assert result.tick == 0

    def test_successful_tick_has_ten_phases(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        result = sim.run_tick()
        # The 10-phase kernel
        assert "ingest" in result.phases_completed
        assert "freeze" in result.phases_completed
        assert "schedule" in result.phases_completed
        assert "observe" in result.phases_completed
        assert "decide" in result.phases_completed
        assert "validate" in result.phases_completed
        assert "act" in result.phases_completed
        assert "commit" in result.phases_completed
        assert "publish" in result.phases_completed
        assert "audit" in result.phases_completed
        assert len(result.phases_completed) == 10

    def test_rolled_back_tick_returns_not_committed(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        # Stage a failing effect to trigger rollback
        from my_team.transaction import EffectType
        sim.task_tree.create(
            task_id="task.trigger",
            title="T",
            creator_agent_id="agent.root",
            owner_agent_id="agent.root",
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.trigger",  # duplicate → fails
            data={
                "task_id": "task.trigger",
                "title": "Dup",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.root",
            },
        )
        result = sim.run_tick()
        assert result.committed is False
        assert len(result.errors) > 0

    def test_phases_completed_matches_last_tick_phases(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        result = sim.run_tick()
        assert result.phases_completed == sim.last_tick_phases


class TestTickEngineClockOnly:
    """TickEngine is a pure clock with no phase logic."""

    def test_advance_returns_none(self) -> None:
        from my_team.tick_engine import TickEngine
        engine = TickEngine()
        result = engine.advance(1)
        assert result is None

    def test_no_snapshots(self) -> None:
        from my_team.tick_engine import TickEngine
        engine = TickEngine()
        engine.advance(3)
        assert not hasattr(engine, '_snapshots') or not engine._snapshots

    def test_no_history(self) -> None:
        from my_team.tick_engine import TickEngine
        engine = TickEngine()
        engine.advance(2)
        assert not hasattr(engine, '_tick_history') or not engine._tick_history
