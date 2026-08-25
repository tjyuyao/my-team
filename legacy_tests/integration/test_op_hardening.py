"""Pending-operation hardening through the simulation (v0.6.0 review P1).

Verifies the timeout/retry/budget/scope/cancel guarantees:

- TIMEOUT → agent woken with a structured error → agent decides
  retry/fail/escalate (retry creates a NEW request_id)
- Per-agent LLM budget enforced in Phase 6 (Validate)
- Duplicate request_id rejected (intra-plan and cross-tick)
- An op cannot escape its agent's scope
- A cancelled op's late result never reaches the agent
"""

from __future__ import annotations

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SubmitLLMRequest
from my_team.pending_ops import OpStatus, OpType
from my_team.simulation import Simulation, SimulationConfig


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


class TimeoutRetryAgent(BaseAgent):
    """Submits an LLM request; on timeout error retries once, then gives up."""

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        if (
            continuation is not None
            and continuation.phase == ContinuationPhase.PROCESSING_RESULT
            and continuation.last_llm_result.get("timed_out")
        ):
            if continuation.total_llm_calls >= 2:
                return []  # give up after one retry
            return [SubmitLLMRequest(
                agent_id=self._agent_id,
                messages=(),
                timeout_ticks=1,
            )]
        return [SubmitLLMRequest(
            agent_id=self._agent_id,
            messages=(),
            timeout_ticks=1,
        )]


class TestTimeoutWake:
    """A timed-out op wakes the agent with a structured error."""

    def test_timeout_wakes_agent_with_error(self) -> None:
        """Deadline passes → op TIMED_OUT → agent woken with error result."""
        sim = Simulation(agent_tree=_make_tree())
        agent = TimeoutRetryAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: submit (timeout_ticks=1 → deadline = tick 1)
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.state == AgentState.WAITING_FOR_LLM

        # Tick 1: deadline not yet past
        sim.run_tick()
        assert len(sim._pending_ops.get_by_agent("agent.root")) == 1

        # Tick 2: timed out → error delivered via continuation → wake
        sim.run_tick()
        assert rs.continuation.react_turn == 1  # error result processed
        # error was structured
        timeouts = [
            e for e in sim.audit_log.for_event_type(AuditEventType.TOOL_RESULT)
            if e.details.get("status") == "timed_out"
        ]
        assert len(timeouts) == 1
        assert "timed out" in (timeouts[0].error or "")

    def test_timeout_retry_creates_new_request_id(self) -> None:
        """Agent retries after the timeout error → NEW request_id."""
        sim = Simulation(agent_tree=_make_tree())
        agent = TimeoutRetryAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: first submission
        sim.run_tick()
        op1 = sim._pending_ops.get_by_agent("agent.root")[0]

        # Ticks 1-2: timeout → agent woken → retries with a fresh request
        sim.run_tick()
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.root"]
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1
        op2 = ops[0]
        assert op2.request_id != op1.request_id
        assert op2.metadata.get("request_id") != op1.metadata.get("request_id")
        assert rs.continuation.pending_request_id == op2.request_id

        # Tick 4: second timeout → agent gives up → back to idle
        sim.run_tick()
        sim.run_tick()
        assert sim._pending_ops.get_by_agent("agent.root") == []
        assert rs.state == AgentState.IDLE
        assert rs.continuation.react_turn == 2


class TestBudget:
    def test_pending_operation_budget_enforced(self) -> None:
        """Per-agent LLM budget: excess SubmitLLMRequests rejected in Validate."""
        sim = Simulation(
            agent_tree=_make_tree(),
            config=SimulationConfig(max_concurrent_llm_requests=1),
        )
        agent = TimeoutRetryAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # An LLM request is already in flight (e.g. from a previous tick)
        sim._pending_ops.submit(
            op_type=OpType.LLM_REQUEST,
            agent_id="agent.root",
            created_tick=0,
            eligible_tick=1,
        )

        # Agent's new submission is rejected at validation
        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1  # only the pre-existing one

        denied = [
            d for d in sim.audit_log.for_event_type(AuditEventType.PERMISSION_DENIED)
            if d.details.get("reason") == "llm_budget_exceeded"
        ]
        assert len(denied) == 1


class TestDuplicateRequestId:
    def test_duplicate_request_id_rejected(self) -> None:
        """An agent cannot reuse a request_id twice in one plan."""
        sim = Simulation(agent_tree=_make_tree())

        class DupAgent(BaseAgent):
            def decide_intents(self, observation, continuation=None) -> list[Intent]:
                return [
                    SubmitLLMRequest(
                        agent_id=self._agent_id, messages=(),
                        request_id="req.dup",
                    ),
                    SubmitLLMRequest(
                        agent_id=self._agent_id, messages=(),
                        request_id="req.dup",
                    ),
                ]

        agent = DupAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1  # second submission rejected

        denied = [
            d for d in sim.audit_log.for_event_type(AuditEventType.PERMISSION_DENIED)
            if d.details.get("reason") == "duplicate_request_id"
        ]
        assert len(denied) == 1


class TestScope:
    def test_pending_operation_cannot_escape_agent_scope(self) -> None:
        """An op is delivered only to the agent it was registered under."""
        sim = Simulation(agent_tree=_make_tree())

        class RecordingAgent(BaseAgent):
            def __init__(self, agent_id: str, **kwargs: object) -> None:
                super().__init__(agent_id=agent_id, **kwargs)
                self.received_results: list[dict] = []

            def decide_intents(self, observation, continuation=None) -> list[Intent]:
                if (
                    continuation is not None
                    and continuation.phase == ContinuationPhase.PROCESSING_RESULT
                    and continuation.last_llm_result
                ):
                    self.received_results.append(dict(continuation.last_llm_result))
                    return []
                return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]

        root = RecordingAgent("agent.root")
        research = RecordingAgent("agent.research")
        for a in (root, research):
            a._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = root
        sim._runtimes["agent.research"] = research

        # Tick 0: root submits; research stays idle
        sim.run_tick()
        op = sim._pending_ops.get_by_agent("agent.root")[0]

        # Tick 1: result arrives → delivered to root's continuation only
        sim._pending_ops.complete(op.request_id, result={"content": "for root"})
        sim.run_tick()

        assert root.received_results == [{"content": "for root"}]
        assert research.received_results == []
        assert sim._agent_runtime_states["agent.research"].continuation.phase == \
            ContinuationPhase.FRESH


class TestCancelNoPublish:
    def test_cancelled_operation_does_not_publish_result(self) -> None:
        """A cancelled op's late result never reaches the agent."""
        sim = Simulation(agent_tree=_make_tree())

        class RecordingAgent(BaseAgent):
            def __init__(self, agent_id: str, **kwargs: object) -> None:
                super().__init__(agent_id=agent_id, **kwargs)
                self.received_results: list[dict] = []

            def decide_intents(self, observation, continuation=None) -> list[Intent]:
                if (
                    continuation is not None
                    and continuation.phase == ContinuationPhase.PROCESSING_RESULT
                    and continuation.last_llm_result
                ):
                    self.received_results.append(dict(continuation.last_llm_result))
                    return []
                return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]

        agent = RecordingAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()
        op = sim._pending_ops.get_by_agent("agent.root")[0]

        # Cancel, then a late result arrives — it must not resurrect the op
        sim._pending_ops.cancel(op.request_id)
        sim._pending_ops.complete(op.request_id, result={"content": "late"})
        assert sim._pending_ops.get_by_id(op.request_id).status == OpStatus.CANCELLED

        sim.run_tick()
        assert agent.received_results == []
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.last_llm_result == {}
