"""End-to-end test of the async LLM pipeline with a deterministic fake provider.

Proves the full continuation-based ReAct loop:

  tick 0: Agent submits SubmitLLMRequest → WAITING_FOR_LLM
  tick 1: FakeLLMProvider completes op → Ingest delivers → agent re-activated
          → parses LLM response into Intents → stages effects
  tick 2: Committed effects visible (task/email/file)

Key assertions:
- Agent never blocks inside decide_intents
- Continuation tracks phase/react_turn/llm_calls
- LLM response → Intents → staged effects → committed state
"""

from __future__ import annotations

import json

from my_team.agent_runtime import (
    AgentObservation,
    BaseAgent,
    action_plan_to_intents,
)
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.fake_llm import FakeLLMProvider
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SubmitLLMRequest
from my_team.pending_ops import OpStatus
from my_team.prompt_templates import PromptTemplates
from my_team.simulation import Simulation


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


class FakeLLMAgent(BaseAgent):
    """Agent that parses LLM responses into Intents using PromptTemplates.

    Mirrors LLMAgent.decide_intents() but uses the FakeLLMProvider
    scripted responses, which arrive via the continuation.
    """

    def __init__(self, agent_id: str, **kwargs: object) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self._templates = PromptTemplates()

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        # Process pending LLM result
        if (
            continuation is not None
            and continuation.phase == ContinuationPhase.PROCESSING_RESULT
            and continuation.last_llm_result
        ):
            result = continuation.last_llm_result
            plan = self._templates.parse_llm_response(
                content=result.get("content", ""),
                tool_calls=list(result.get("tool_calls", [])),
                agent_id=self._agent_id,
                tick=observation.tick,
            )
            return action_plan_to_intents(plan)

        # No pending result — submit async request
        return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]


class TestAsyncLLMFlow:
    """Async LLM request → response → re-activation → effects."""

    def test_submit_then_receive(self) -> None:
        """Full async cycle: submit at tick 0, receive at tick 1, act on result."""
        sim = Simulation(agent_tree=_make_tree())
        provider = FakeLLMProvider(
            responses={
                "agent.root": [
                    {
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": "delegate",
                                "arguments": json.dumps({
                                    "recipient_agent_id": "agent.research",
                                    "task_title": "Async Task",
                                    "task_description": "via async LLM",
                                }),
                            },
                        }],
                    },
                ],
            },
        )

        agent = FakeLLMAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: submit LLM request
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.state == AgentState.WAITING_FOR_LLM
        assert rs.continuation.phase == ContinuationPhase.WAITING_FOR_LLM
        assert rs.continuation.total_llm_calls == 1

        # Provider completes the op before tick 1
        completed = provider.advance(sim, current_tick=1)
        assert completed == 1

        # Tick 1: Ingest delivers → agent re-activated → parses → delegates
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.root"]

        # After processing result: continuation moved forward
        assert rs.continuation.react_turn >= 1
        # Agent committed the delegation effect
        assert len(sim.task_tree.get_active_tasks()) == 1
        assert len(sim._mail_system._all_emails) == 1

        # Agent back to idle after completing this activation
        assert rs.state == AgentState.IDLE

    def test_continuation_tracks_calls(self) -> None:
        """Continuation should track llm_calls and react_turn across ticks."""
        sim = Simulation(agent_tree=_make_tree())
        provider = FakeLLMProvider(
            responses={
                "agent.root": [
                    {"content": "", "tool_calls": []},  # empty response
                ],
            },
        )

        agent = FakeLLMAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: submit
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.total_llm_calls == 1

        # Tick 1: receive + process (empty → no intents)
        provider.advance(sim, current_tick=1)
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.react_turn == 1
        # Phase should be back to fresh (processed the result, no more work)
        assert rs.continuation.phase in {
            ContinuationPhase.FRESH,
            ContinuationPhase.READY_TO_DECIDE,
        }

    def test_op_lifecycle_in_registry(self) -> None:
        """Pending op transitions SUBMITTED → PENDING → COMPLETED."""
        sim = Simulation(agent_tree=_make_tree())
        provider = FakeLLMProvider(
            responses={"agent.root": [{"content": "hello", "tool_calls": []}]},
        )

        agent = FakeLLMAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1
        assert ops[0].status == OpStatus.SUBMITTED

        provider.advance(sim, current_tick=1)
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert ops[0].status == OpStatus.COMPLETED

    def test_llm_timeout(self) -> None:
        """LLM op that never responds is marked TIMED_OUT."""
        sim = Simulation(agent_tree=_make_tree())
        # Provider with long latency never completes within the test window
        provider = FakeLLMProvider(latency_ticks=100)

        agent = FakeLLMAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: submit with default timeout_ticks=10
        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1

        # Provider won't complete (latency 100 > test window)
        provider.advance(sim, current_tick=1)
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1

        # Mark as PENDING and expire the deadline
        op = sim._pending_ops.get_by_agent("agent.root")[0]
        op.status = OpStatus.PENDING
        op.deadline_tick = 5
        expired = sim._pending_ops.timeout_expired(6)
        assert len(expired) == 1
        assert expired[0].status == OpStatus.TIMED_OUT

    def test_fake_provider_register_script(self) -> None:
        """register_script overrides responses per agent."""
        provider = FakeLLMProvider()
        provider.register_script("agent.a", [{"content": "first"}, {"content": "second"}])
        assert provider._next_response("agent.a") == {"content": "first"}
        assert provider._next_response("agent.a") == {"content": "second"}
        # Exhausted → empty
        assert provider._next_response("agent.a") == {"content": "", "tool_calls": []}


class TestFullDelegationLoopAsync:
    """Two-hop delegation with async LLM at each hop."""

    def test_root_async_delegates_to_research(self) -> None:
        """Root uses async LLM to decide, then delegates via LLM-parsed intent."""
        sim = Simulation(agent_tree=_make_tree())
        provider = FakeLLMProvider(
            latency_ticks=1,
            responses={
                "agent.root": [
                    {
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": "delegate",
                                "arguments": json.dumps({
                                    "recipient_agent_id": "agent.research",
                                    "task_title": "Research Task",
                                    "task_description": "Gather data",
                                }),
                            },
                        }],
                    },
                ],
            },
        )

        agent = FakeLLMAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: submit LLM
        sim.run_tick()
        assert sim._agent_runtime_states["agent.root"].state == AgentState.WAITING_FOR_LLM

        # Tick 1: receive → delegate → task created + email queued
        provider.advance(sim, current_tick=1)
        sim.run_tick()

        assert len(sim.task_tree.get_active_tasks()) == 1
        task = sim.task_tree.get_active_tasks()[0]
        assert task.title == "Research Task"
        assert task.assignee_agent_id == "agent.research"

        # Email committed with deliver_at_tick = tick 1 + 1 = 2
        emails = list(sim._mail_system._all_emails.values())
        assert len(emails) == 1
        assert emails[0].deliver_at_tick == 2

        # Tick 2: email delivered → NEW_EMAIL event → research activated
        sim.run_tick()
        history = sim.scheduler.get_activation_history()
        research_activations = [a for a in history if a.agent_id == "agent.research"]
        assert len(research_activations) >= 1
