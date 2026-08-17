"""End-to-end test of the async tool request pipeline.

Proves the continuation-based tool loop:

  tick 0: Agent submits SubmitToolRequest(remote tool) → WAITING_FOR_TOOL
  tick 1: FakeToolExecutor completes op → Ingest delivers → agent re-activated
          → parses tool result → produces next Intents

Key assertions:
- Remote tools go through PendingOperationRegistry
- Agent → WAITING_FOR_TOOL → re-activated on TOOL_RESULT
- Tool result delivered via continuation (last_tool_result)
- Tool timeout marked TIMED_OUT
"""

from __future__ import annotations

from my_team.agent_runtime import (
    AgentObservation,
    BaseAgent,
)
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.fake_llm import FakeToolExecutor
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import (
    Intent,
    SendEmailIntent,
    SubmitLLMRequest,
    SubmitToolRequest,
)
from my_team.pending_ops import OpStatus, OpType
from my_team.simulation import Simulation


def _bootstrap_agent(sim: Simulation, agent_id: str) -> None:
    """Enqueue a BOOTSTRAP event for a non-bootstrap agent."""
    from my_team.models.activation import WakeCondition, WakeEventType, WakeupEvent
    cond = sim.scheduler.get_wake_condition(agent_id)
    sim.scheduler.update_wake_condition(
        agent_id,
        WakeCondition(
            event_types=cond.event_types | {WakeEventType.BOOTSTRAP},
            wake_at_tick=0,
        ),
    )
    sim.scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.BOOTSTRAP,
        target_agent_id=agent_id,
        tick=0,
        source_agent_id="system",
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
                "tools": ["read", "write", "ls", "send_email", "web_search"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


class ToolFlowAgent(BaseAgent):
    """Agent with a scripted multi-step tool flow.

    State machine in decide_intents:
      1. No pending result → SubmitToolRequest("web_search")
      2. Tool result received → SendEmailIntent (report back)
    """

    def __init__(self, agent_id: str, **kwargs: object) -> None:
        super().__init__(agent_id=agent_id, **kwargs)

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        if (
            continuation is not None
            and continuation.phase == ContinuationPhase.PROCESSING_RESULT
            and continuation.last_tool_result
        ):
            # Tool result received — report back via email
            result = continuation.last_tool_result
            summary = result.get("summary", result.get("error", "done"))
            return [
                SendEmailIntent(
                    agent_id=self._agent_id,
                    to=["agent.research"],
                    subject="[RESULT] Search Complete",
                    body=f"Search result: {summary}",
                ),
            ]
        # No pending result — submit async tool request
        return [
            SubmitToolRequest(
                agent_id=self._agent_id,
                tool_name="web_search",
                arguments={"query": "market trends"},
                timeout_ticks=10,
            ),
        ]


class TestAsyncToolFlow:
    """SubmitToolRequest → PendingOperation → TOOL_RESULT → re-activation."""

    def _setup(self) -> tuple[Simulation, FakeToolExecutor, ToolFlowAgent]:
        sim = Simulation(agent_tree=_make_tree())
        executor = FakeToolExecutor(latency_ticks=1)
        executor.register_result("agent.research", "web_search", [
            {"success": True, "summary": "Market growing 10% YoY"},
        ])

        agent = ToolFlowAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent

        # Bootstrap research (root bootstraps by default; research needs a nudge)
        _bootstrap_agent(sim, "agent.research")
        return sim, executor, agent

    def test_submit_tool_creates_pending_op(self) -> None:
        """SubmitToolRequest registers a TOOL_REQUEST pending op."""
        sim, executor, agent = self._setup()

        sim.run_tick()

        rs = sim._agent_runtime_states["agent.research"]
        assert rs.state == AgentState.WAITING_FOR_TOOL
        assert rs.continuation.phase == ContinuationPhase.WAITING_FOR_TOOL
        assert rs.continuation.total_tool_calls == 1

        ops = sim._pending_ops.get_by_agent("agent.research")
        assert len(ops) == 1
        assert ops[0].op_type == OpType.TOOL_REQUEST
        assert ops[0].metadata.get("tool_name") == "web_search"

    def test_tool_result_reactivates_agent(self) -> None:
        """Tool result delivered → agent re-activated → sends result email."""
        sim, executor, agent = self._setup()

        # Tick 0: submit tool request
        sim.run_tick()
        assert sim._agent_runtime_states["agent.research"].state == AgentState.WAITING_FOR_TOOL

        # Tick 1: executor completes → Ingest delivers → agent acts on result
        completed = executor.advance(sim, current_tick=1)
        assert completed == 1

        sim.run_tick()
        rs = sim._agent_runtime_states["agent.research"]

        # Agent processed the tool result and sent an email
        assert len(sim._mail_system._all_emails) == 1
        email = list(sim._mail_system._all_emails.values())[0]
        assert email.subject == "[RESULT] Search Complete"
        assert "Market growing" in email.body

        # Continuation tracked the tool call
        assert rs.continuation.total_tool_calls == 1
        assert rs.continuation.react_turn == 1
        assert rs.state == AgentState.IDLE

    def test_tool_timeout(self) -> None:
        """Tool op that never responds is marked TIMED_OUT."""
        sim, executor, agent = self._setup()
        # Executor with long latency never completes in the test window
        executor._latency_ticks = 100

        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.research")
        assert len(ops) == 1

        # Mark pending and expire
        op = sim._pending_ops.get_by_agent("agent.research")[0]
        op.status = OpStatus.PENDING
        op.deadline_tick = 5
        expired = sim._pending_ops.timeout_expired(6)
        assert len(expired) == 1
        assert expired[0].status == OpStatus.TIMED_OUT

    def test_tool_error_result(self) -> None:
        """Tool error result is delivered to the agent's continuation."""
        sim = Simulation(agent_tree=_make_tree())
        executor = FakeToolExecutor(latency_ticks=1)
        executor.register_result("agent.research", "web_search", [
            {"success": False, "error": "Rate limited"},
        ])
        agent = ToolFlowAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap_agent(sim, "agent.research")

        sim.run_tick()
        executor.advance(sim, current_tick=1)
        sim.run_tick()

        # Agent received the error result and reported it
        assert len(sim._mail_system._all_emails) == 1
        email = list(sim._mail_system._all_emails.values())[0]
        assert "Rate limited" in email.body


class TestHybridAsyncFlow:
    """LLM → tool → LLM multi-hop async flow."""

    def test_llm_then_tool_then_result(self) -> None:
        """Full continuation: LLM decides → tool request → tool result → email."""
        from my_team.fake_llm import FakeLLMProvider

        sim = Simulation(agent_tree=_make_tree())
        llm = FakeLLMProvider(latency_ticks=1)
        tool = FakeToolExecutor(latency_ticks=1)
        tool.register_result("agent.research", "web_search", [
            {"success": True, "summary": "Data found"},
        ])

        # Script: after LLM, agent decides to use web_search tool
        class HybridAgent(BaseAgent):
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
                    # LLM said "use web_search" → submit tool request
                    return [
                        SubmitToolRequest(
                            agent_id=self._agent_id,
                            tool_name="web_search",
                            arguments={"query": "q"},
                            timeout_ticks=10,
                        ),
                    ]
                if (
                    continuation is not None
                    and continuation.phase == ContinuationPhase.PROCESSING_RESULT
                    and continuation.last_tool_result
                ):
                    # Tool result → report via email
                    return [
                        SendEmailIntent(
                            agent_id=self._agent_id,
                            to=["agent.research"],
                            subject="[RESULT] Done",
                            body="Tool returned data",
                        ),
                    ]
                return [
                    SubmitLLMRequest(agent_id=self._agent_id, messages=()),
                ]

        agent = HybridAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap_agent(sim, "agent.research")

        # Tick 0: LLM request submitted
        sim.run_tick()
        assert sim._agent_runtime_states["agent.research"].state == AgentState.WAITING_FOR_LLM

        # Tick 1: LLM completes → agent submits tool request
        llm.advance(sim, current_tick=1)
        sim.run_tick()
        assert sim._agent_runtime_states["agent.research"].state == AgentState.WAITING_FOR_TOOL

        # Tick 2: tool completes → agent sends result email
        tool.advance(sim, current_tick=2)
        sim.run_tick()
        assert len(sim._mail_system._all_emails) == 1
        email = list(sim._mail_system._all_emails.values())[0]
        assert email.subject == "[RESULT] Done"

        # Full continuation tracked
        rs = sim._agent_runtime_states["agent.research"]
        assert rs.continuation.total_llm_calls == 1
        assert rs.continuation.total_tool_calls == 1
        assert rs.continuation.react_turn == 2
