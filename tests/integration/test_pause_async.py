"""Tests for pause semantics in the async model (SPEC §8.6).

Verifies:
- pause() takes effect at the next commit boundary (no mid-tick)
- No new activations while paused
- External requests continue; results are quarantined (not applied)
- Results are applied after resume
"""

from __future__ import annotations

import pytest

from my_team.agent_runtime import (
    AgentObservation,
    BaseAgent,
)
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.fake_llm import FakeLLMProvider
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SubmitLLMRequest, WritePrivateFileIntent
from my_team.pending_ops import OpStatus
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
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


class PauseAgent(BaseAgent):
    """Agent: no result → SubmitLLMRequest; has result → write file."""

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        if (
            continuation is not None
            and continuation.phase == ContinuationPhase.PROCESSING_RESULT
            and continuation.last_llm_result
        ):
            content = continuation.last_llm_result.get("content", "")
            return [
                WritePrivateFileIntent(
                    agent_id=self._agent_id,
                    path="result.txt",
                    content=content,
                ),
            ]
        return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]


class TestPauseSemantics:
    """Pause at commit boundary with async external operations."""

    def test_pause_stops_tick_advancement(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.pause()
        assert sim.is_paused
        with pytest.raises(RuntimeError, match="paused"):
            sim.run_tick()

    def test_pause_no_new_activations(self) -> None:
        """No agents activate while paused."""
        sim = Simulation(agent_tree=_make_tree())
        agent = PauseAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0 runs normally
        sim.run_tick()
        assert len(sim.scheduler.get_activation_history()) >= 1

        # Pause — no new activations
        sim.pause()
        with pytest.raises(RuntimeError):
            sim.run_tick()
        assert sim.current_tick == 1  # not advanced

    def test_external_result_quarantined_while_paused(self) -> None:
        """LLM result arrives while paused → NOT applied to agent state."""
        from uuid import uuid4

        sim = Simulation(agent_tree=_make_tree())
        filename = f"q_{uuid4().hex[:8]}.txt"
        provider = FakeLLMProvider(
            latency_ticks=1,
            responses={"agent.root": [{"content": "quarantined", "tool_calls": []}]},
        )

        class QuarantineAgent(PauseAgent):
            def decide_intents(self, observation, continuation=None):
                intents = super().decide_intents(observation, continuation)
                for i in intents:
                    if hasattr(i, "path"):
                        i.path = filename
                return intents

        agent = QuarantineAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: submit LLM request
        sim.run_tick()
        assert sim._agent_runtime_states["agent.root"].state == AgentState.WAITING_FOR_LLM

        # Pause; provider completes the op while paused
        sim.pause()
        provider.advance(sim, current_tick=1)

        # Result is in the registry (COMPLETED) but NOT delivered:
        # the agent's continuation has no result, file not written
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.last_llm_result == {}
        assert not (sim._private_store.agent_home("agent.root") / filename).exists()

        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1
        assert ops[0].status == OpStatus.COMPLETED  # quarantined, not consumed

    def test_resume_applies_quarantined_result(self) -> None:
        """After resume, the quarantined result is delivered and applied."""
        from uuid import uuid4

        sim = Simulation(agent_tree=_make_tree())
        filename = f"r_{uuid4().hex[:8]}.txt"
        provider = FakeLLMProvider(
            latency_ticks=1,
            responses={"agent.root": [{"content": "after resume", "tool_calls": []}]},
        )

        class ResumeAgent(PauseAgent):
            def decide_intents(self, observation, continuation=None):
                intents = super().decide_intents(observation, continuation)
                for i in intents:
                    if hasattr(i, "path"):
                        i.path = filename
                return intents

        agent = ResumeAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()  # tick 0: submit
        sim.pause()
        provider.advance(sim, current_tick=1)  # completes while paused

        # Resume and run next tick
        sim.resume()
        assert not sim.is_paused
        sim.run_tick()  # tick 1: Ingest delivers → agent writes file

        result_file = sim._private_store.agent_home("agent.root") / filename
        assert result_file.exists()
        assert result_file.read_text() == "after resume"
        # Quarantined op consumed
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 0
