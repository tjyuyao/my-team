"""Tests for T5: SimulationRuntime.

Date: 2026-08-18
"""

from __future__ import annotations

import time

from my_team.agent_tree import AgentTree
from my_team.runtime import SimulationRuntime
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


class TestSimulationRuntime:
    def test_step_executes_ticks(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        results = runtime.step(3)
        assert len(results) == 3
        assert sim._tick_engine.current_tick == 3

    def test_step_zero_returns_empty(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        results = runtime.step(0)
        assert len(results) == 0
        assert sim._tick_engine.current_tick == 0

    def test_pause_prevents_step(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        runtime.step(1)
        runtime.pause()
        results = runtime.step(3)
        assert len(results) == 0
        assert sim._tick_engine.current_tick == 1

    def test_resume_allows_step(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        runtime.step(1)
        runtime.pause()
        runtime.resume()
        results = runtime.step(2)
        assert len(results) == 2
        assert sim._tick_engine.current_tick == 3

    def test_set_tick_duration(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim, tick_duration_seconds=1.0)
        assert runtime.tick_duration == 1.0
        runtime.set_tick_duration(0.5)
        assert runtime.tick_duration == 0.5

    def test_set_tick_duration_minimum(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        runtime.set_tick_duration(0.001)
        assert runtime.tick_duration == 0.01  # clamped to minimum

    def test_status_returns_dict(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        status = runtime.status
        assert "tick" in status
        assert "state" in status
        assert "running" in status
        assert status["tick"] == 0
        assert status["running"] is False

    def test_status_after_step(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        runtime.step(2)
        status = runtime.status
        assert status["tick"] == 2
        assert status["ticks_completed"] == 2

    def test_start_stop_lifecycle(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim, tick_duration_seconds=0.05)
        runtime.start()
        time.sleep(0.2)  # let a few ticks run
        runtime.stop()
        assert sim._tick_engine.current_tick >= 1

    def test_start_is_idempotent(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim, tick_duration_seconds=0.05)
        runtime.start()
        runtime.start()  # no-op
        time.sleep(0.1)
        runtime.stop()

    def test_stop_when_not_running(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        runtime.stop()  # no-op, should not raise

    def test_repr(self):
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        r = repr(runtime)
        assert "SimulationRuntime" in r
        assert "running=False" in r
