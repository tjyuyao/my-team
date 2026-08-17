"""Tests for T5: LLM Dispatcher.

Date: 2026-08-18
"""

from __future__ import annotations

import time
from typing import Any

from my_team.agent_tree import AgentTree
from my_team.llm_dispatcher import LLMDispatcher
from my_team.models.activation import ReadyCandidate
from my_team.models.intent import SubmitLLMRequest
from my_team.models.llm import LLMResult
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


class FakeGateway:
    """Minimal fake LLMGateway for testing the dispatcher."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None):
        self._responses = responses or {}
        self._call_count = 0
        self._fail = False

    def complete(self, request: Any) -> LLMResult:
        self._call_count += 1
        if self._fail:
            raise RuntimeError("Fake gateway error")
        agent_id = request.agent_id
        resp = self._responses.get(agent_id, {"content": "fake response"})
        return LLMResult(
            content=resp.get("content", ""),
            tool_calls=resp.get("tool_calls", []),
            usage=resp.get("usage", {"prompt_tokens": 10, "completion_tokens": 5}),
            model="fake-model",
            finish_reason="stop",
        )


class TestLLMDispatcher:
    def _submit_llm_op(self, sim: Simulation, agent_id: str = "agent.root") -> str:
        """Helper: submit an LLM request and return the request_id."""
        intent = SubmitLLMRequest(
            agent_id=agent_id,
            messages=({"role": "user", "content": "test"},),
            timeout_ticks=10,
        )
        plan = {"agent.root": [intent]}
        from my_team.agent_runtime import ActionResult, AgentAction
        validated = {"agent.root": [ActionResult(
            action=AgentAction(
                action_type="submit_llm_request", tool_name="",
                payload=dict(intent.payload),
            ),
            success=True, result_data={"validated": True},
        )]}
        sim._phase_act(
            0, plan,
            ready=[ReadyCandidate(agent_id=agent_id, events=(), tick=0)],
            validated=validated,
        )
        ops = sim._pending_ops.get_by_agent(agent_id)
        assert len(ops) == 1
        return ops[0].request_id

    def test_dispatcher_completes_llm_op(self):
        sim = Simulation(agent_tree=_make_tree())
        gateway = FakeGateway()
        dispatcher = LLMDispatcher(sim, gateway, poll_interval=0.05)

        req_id = self._submit_llm_op(sim)
        assert sim._pending_ops.get_by_id(req_id).status.value == "submitted"

        dispatcher._running = True  # enable polling
        dispatcher._poll_once()

        assert gateway._call_count == 1
        op = sim._pending_ops.get_by_id(req_id)
        assert op.status.value == "completed"

    def test_dispatcher_handles_gateway_error(self):
        sim = Simulation(agent_tree=_make_tree())
        gateway = FakeGateway()
        gateway._fail = True
        dispatcher = LLMDispatcher(sim, gateway, poll_interval=0.05)

        req_id = self._submit_llm_op(sim)
        dispatcher._running = True
        dispatcher._poll_once()

        op = sim._pending_ops.get_by_id(req_id)
        assert op.status.value == "failed"
        assert dispatcher._error_count == 1

    def test_dispatcher_ignores_tool_ops(self):
        sim = Simulation(agent_tree=_make_tree())
        gateway = FakeGateway()
        dispatcher = LLMDispatcher(sim, gateway, poll_interval=0.05)
        dispatcher._running = True

        # Register a remote tool and submit a tool op
        from tests.tool_helpers import register_remote_tool
        register_remote_tool(sim, "remote_calc")
        from my_team.models.intent import SubmitToolRequest
        intent = SubmitToolRequest(
            agent_id="agent.root", tool_name="remote_calc",
            arguments={"x": 1}, timeout_ticks=10,
        )
        plan = {"agent.root": [intent]}
        from my_team.agent_runtime import ActionResult, AgentAction
        validated = {"agent.root": [ActionResult(
            action=AgentAction(
                action_type="submit_tool_request", tool_name="remote_calc",
                payload=dict(intent.payload),
            ),
            success=True, result_data={"validated": True},
        )]}
        sim._phase_act(
            0, plan,
            ready=[ReadyCandidate(agent_id="agent.root", events=(), tick=0)],
            validated=validated,
        )

        dispatcher._poll_once()
        # Gateway should NOT have been called for tool ops
        assert gateway._call_count == 0

    def test_fake_provider_still_works(self):
        """FakeLLMProvider.advance() still works alongside dispatcher."""
        from my_team.fake_llm import FakeLLMProvider
        sim = Simulation(agent_tree=_make_tree())
        provider = FakeLLMProvider(
            responses={"agent.root": [{"content": "ok"}]},
            latency_ticks=1,
        )
        req_id = self._submit_llm_op(sim)

        # Provider completes it
        completed = provider.advance(sim, current_tick=1)
        assert completed == 1

        op = sim._pending_ops.get_by_id(req_id)
        assert op.status.value == "completed"

    def test_dispatcher_start_stop(self):
        sim = Simulation(agent_tree=_make_tree())
        gateway = FakeGateway()
        dispatcher = LLMDispatcher(sim, gateway, poll_interval=0.05)
        dispatcher.start()
        time.sleep(0.15)
        dispatcher.stop()
        assert not dispatcher._running

    def test_dispatcher_processed_count(self):
        sim = Simulation(agent_tree=_make_tree())
        gateway = FakeGateway()
        dispatcher = LLMDispatcher(sim, gateway, poll_interval=0.05)

        self._submit_llm_op(sim)
        dispatcher._running = True
        dispatcher._poll_once()
        assert dispatcher.processed_count == 1
