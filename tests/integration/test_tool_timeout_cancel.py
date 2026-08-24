"""Tool timeout & cancel tests (v0.7.0 P1-4).

Verifies:
- Local sandboxed tools return structured error_code='tool_timeout'
  when the subprocess times out (process group killed) and audit
  TOOL_TIMEOUT
- cancel_operation: manifest supports_cancel gate, agent-wake with a
  structured cancellation notice (never the result), OP_CANCELLED
  audit, late results fenced (never published)
- Non-cancellable / wrong-agent / terminal ops refuse cancellation
"""

from __future__ import annotations

from my_team.agent_runtime import BaseAgent, ToolContext
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.activation import WakeEventType
from my_team.models.continuation import ContinuationPhase
from my_team.models.intent import Intent, SubmitLLMRequest, SubmitToolRequest
from my_team.pending_ops import OpStatus, OpType
from my_team.sandbox_tools import run_sandboxed_process
from my_team.simulation import Simulation
from tests.tool_helpers import register_remote_tool


def _make_tree(tools: list[str]) -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": tools,
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["run_tests", "web_search", "web_nocancel"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _bootstrap(sim: Simulation, agent_id: str) -> None:
    """Enqueue a BOOTSTRAP event for a non-bootstrap agent."""
    from my_team.models.activation import WakeCondition, WakeupEvent
    cond = sim.scheduler.get_wake_condition(agent_id)
    sim.scheduler.update_wake_condition(
        agent_id,
        WakeCondition(
            event_types=cond.event_types | {WakeEventType.BOOTSTRAP},
            wake_at_tick=0, visible_at_tick=0,
        ),
    )
    sim.scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.BOOTSTRAP,
        target_agent_id=agent_id,
        tick=0, visible_at_tick=0,
        source_agent_id="system",
    ))


class ToolSubmitAgent(BaseAgent):
    """Submits one tool request per activation (async)."""

    def __init__(self, agent_id: str, tool_name: str, **kwargs: object) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self._tool_name = tool_name

    def decide_intents(
        self,
        observation,
        continuation=None,
    ) -> list[Intent]:
        return [SubmitToolRequest(
            agent_id=self._agent_id,
            tool_name=self._tool_name,
            arguments={},
            timeout_ticks=10,
        )]


class LLMSubmitAgent(BaseAgent):
    def decide_intents(self, observation, continuation=None) -> list[Intent]:
        return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]


class TestLocalToolTimeout:
    """Sandboxed local tools: structured tool_timeout + audit."""

    def test_run_tests_timeout_returns_structured_error(self, monkeypatch) -> None:
        sim = Simulation(agent_tree=_make_tree(["run_tests"]))

        def _fake_run(*args, **kwargs) -> dict:
            return {
                "success": False,
                "timed_out": True,
                "exit_code": None,
                "stdout": "",
                "stderr": "killed",
                "duration_ms": 100,
            }

        monkeypatch.setattr(
            "my_team.simulation.run_sandboxed_process", _fake_run,
        )
        ctx = ToolContext(
            agent_id="agent.research", tick=0,
            allowed_tools=frozenset({"run_tests"}),
        )
        result = sim._tool_registry.execute(
            ctx, "run_tests", test_path="",
        )
        assert not result.success
        assert result.error_code == "tool_timeout"
        assert result.retryable is False
        assert result.data["timed_out"] is True
        timed_out = sim.audit_log.for_event_type(AuditEventType.TOOL_TIMEOUT)
        assert len(timed_out) == 1
        assert timed_out[0].details["tool"] == "run_tests"

    def test_timeout_kills_process_group(self) -> None:
        """The underlying subprocess runner kills the whole group."""
        res = run_sandboxed_process(
            ["sleep", "3"], timeout_ms=150,
        )
        assert res["timed_out"] is True
        assert res["exit_code"] is None
        assert res["success"] is False


class TestCancelOperation:
    """cancel_operation: gate, wake, audit, fencing."""

    def _sim_with_pending_tool(
        self, tool: str = "web_search",
    ) -> tuple[Simulation, str]:
        sim = Simulation(agent_tree=_make_tree(
            ["web_search", "web_nocancel"],
        ))
        register_remote_tool(sim, "web_search",
                             supports_cancel=True)
        register_remote_tool(sim, "web_nocancel",
                             supports_cancel=False)
        agent = ToolSubmitAgent("agent.research", tool)
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")
        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.research")
        assert len(ops) == 1
        return sim, ops[0].request_id

    def test_cancel_requires_supports_cancel(self) -> None:
        sim, request_id = self._sim_with_pending_tool("web_nocancel")
        result = sim.cancel_operation(request_id)
        assert not result.accepted
        assert "supports_cancel" in result.reason
        assert result.result_fenced is False
        # Op still in flight, agent still waiting (dispatch may have
        # claimed it: SUBMITTED → PENDING)
        op = sim._pending_ops.get_by_id(request_id)
        assert op is not None
        assert op.status in {OpStatus.SUBMITTED, OpStatus.PENDING}

    def test_cancel_wakes_agent_with_notice(self) -> None:
        sim, request_id = self._sim_with_pending_tool("web_search")
        result = sim.cancel_operation(request_id)
        assert result.accepted
        assert result.result_fenced is True
        assert result.external_effects_possible is True
        assert result.executor_cancel_requested is False  # no executor to signal
        # Op removed from registry
        assert sim._pending_ops.get_by_id(request_id) is None

        # Agent was woken with a cancellation notice (NOT the result)
        rs = sim._agent_runtime_states["agent.research"]
        assert rs.continuation.pending_request_id != request_id
        assert rs.continuation.phase == ContinuationPhase.PROCESSING_RESULT
        last = rs.continuation.last_tool_result or {}
        assert last.get("cancelled") is True
        assert last.get("request_id") == request_id
        assert "error" in last

        # Audited
        cancelled = sim.audit_log.for_event_type(AuditEventType.OP_CANCELLED)
        assert len(cancelled) == 1
        assert cancelled[0].details["request_id"] == request_id

    def test_late_result_never_published(self) -> None:
        sim, request_id = self._sim_with_pending_tool("web_search")
        result = sim.cancel_operation(request_id)
        assert result.accepted and result.result_fenced

        # The executor completes the (cancelled) op late
        completed = sim._pending_ops.complete(request_id, result={"summary": "x"})
        assert completed is None  # removed already → ignored
        # Nothing delivered, no wake event for the result
        rs = sim._agent_runtime_states["agent.research"]
        assert (rs.continuation.last_tool_result or {}).get("summary") is None

    def test_cancel_wrong_agent_refused(self) -> None:
        sim, request_id = self._sim_with_pending_tool("web_search")
        result = sim.cancel_operation(request_id, agent_id="agent.root")
        assert not result.accepted
        assert "belongs to" in result.reason
        assert sim._pending_ops.get_by_id(request_id) is not None

    def test_cancel_missing_op(self) -> None:
        sim = Simulation(agent_tree=_make_tree([]))
        result = sim.cancel_operation("tool.req.nope")
        assert not result.accepted
        assert "not found" in result.reason

    def test_llm_request_cancellable(self) -> None:
        sim = Simulation(agent_tree=_make_tree(["web_search"]))
        agent = LLMSubmitAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")
        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.research")
        assert len(ops) == 1
        assert ops[0].op_type == OpType.LLM_REQUEST

        result = sim.cancel_operation(ops[0].request_id)
        assert result.accepted
        # Logical cancel + fencing — provider-side effects (cost, logs,
        # processing) cannot be undone
        assert result.external_effects_possible is True
        rs = sim._agent_runtime_states["agent.research"]
        assert (rs.continuation.last_llm_result or {}).get("cancelled") is True

    def test_cancel_terminal_op_refused(self) -> None:
        sim, request_id = self._sim_with_pending_tool("web_search")
        sim._pending_ops.get_by_id(request_id).status = OpStatus.TIMED_OUT
        result = sim.cancel_operation(request_id)
        assert not result.accepted
        assert "no longer in flight" in result.reason
