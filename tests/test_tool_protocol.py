"""ToolRequest / ToolResult contract (v0.8.0 P1-3).

- ToolManifest.manifest_hash: stable per contract, changes on version/
  field change
- ToolRequest: built by the kernel at Act with system-injected
  identity (agent_id / manifest_hash / input_hash / state_epoch /
  workspace_version) — never supplied by the executor
- registry.complete_tool: correlation by request_id, contract fields
  recorded for audit, late results for terminal ops ignored
- Ingest: delivered results audited with manifest_hash / tool_version /
  input_hash / output_hash (replay context)
"""

from __future__ import annotations

import uuid
from typing import Any

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.fake_llm import FakeToolExecutor
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SendEmailIntent, SubmitToolRequest
from my_team.pending_ops import OpStatus, OpType, PendingOperationRegistry
from my_team.simulation import Simulation
from my_team.tool_manifest import ExecutionClass, ToolManifest
from my_team.tool_protocol import (
    ToolRequest,
    ToolResultContract,
    canonical_json,
    hash_payload,
)
from tests.tool_helpers import register_remote_tool


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
    """Scripted agent: submit web_search once, report via email."""

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
            result = continuation.last_tool_result
            return [SendEmailIntent(
                agent_id=self._agent_id,
                to=["agent.research"],
                subject="[RESULT] Done",
                body=str(result.get("summary", "")),
            )]
        return [SubmitToolRequest(
            agent_id=self._agent_id,
            tool_name="web_search",
            arguments={"query": "market trends"},
            timeout_ticks=10,
        )]


class TestManifestHash:
    """manifest_hash: stable per contract, changes on any change."""

    def test_hash_stable_across_instances(self) -> None:
        m1 = ToolManifest(name="t", version="1.0.0",
                          execution_class=ExecutionClass.READ_ONLY)
        m2 = ToolManifest(name="t", version="1.0.0",
                          execution_class=ExecutionClass.READ_ONLY)
        assert m1.manifest_hash == m2.manifest_hash
        assert len(m1.manifest_hash) == 64

    def test_hash_changes_on_version_bump(self) -> None:
        m1 = ToolManifest(name="t", version="1.0.0",
                          execution_class=ExecutionClass.READ_ONLY)
        m2 = ToolManifest(name="t", version="1.0.1",
                          execution_class=ExecutionClass.READ_ONLY)
        assert m1.manifest_hash != m2.manifest_hash

    def test_hash_changes_on_contract_field_change(self) -> None:
        m1 = ToolManifest(name="t", version="1.0.0",
                          execution_class=ExecutionClass.READ_ONLY)
        m2 = ToolManifest(name="t", version="1.0.0",
                          execution_class=ExecutionClass.READ_ONLY,
                          max_output_bytes=1_000_000)
        assert m1.manifest_hash != m2.manifest_hash

    def test_hash_payload_deterministic(self) -> None:
        assert hash_payload({"b": 1, "a": [1, 2]}) == hash_payload(
            {"a": [1, 2], "b": 1},
        )
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


class TestToolRequestBuiltAtAct:
    """The kernel builds ToolRequest with system-injected identity."""

    def _submit(self) -> tuple[Simulation, FakeToolExecutor, ToolFlowAgent]:
        sim = Simulation(agent_tree=_make_tree())
        register_remote_tool(sim._tool_registry, "web_search")
        executor = FakeToolExecutor(latency_ticks=1)
        executor.register_result("agent.research", "web_search", [
            {"success": True, "summary": "Market growing"},
        ])
        agent = ToolFlowAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        from my_team.models.activation import (
            WakeCondition,
            WakeEventType,
            WakeupEvent,
        )
        # Preserve the registered condition (must still match TOOL_RESULT
        # wakes) and add BOOTSTRAP — replacing it with {BOOTSTRAP} alone
        # would suppress the result wake.
        cond = sim.scheduler.get_wake_condition("agent.research")
        sim.scheduler.update_wake_condition(
            "agent.research",
            WakeCondition(
                event_types=cond.event_types | {WakeEventType.BOOTSTRAP},
                wake_at_tick=0,
            ),
        )
        sim.scheduler.enqueue_event(WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.research",
            tick=0,
            source_agent_id="system",
        ))
        return sim, executor, agent

    def test_request_fields_system_injected(self) -> None:
        sim, executor, agent = self._submit()
        manifest = sim._tool_registry.get_manifest("web_search")
        assert manifest is not None

        sim.run_tick()

        op = sim._pending_ops.get_by_agent("agent.research")[0]
        assert op.tool_request is not None
        req = op.tool_request
        assert req.request_id == op.request_id
        assert req.agent_id == "agent.research"
        assert req.tool_name == "web_search"
        assert req.tool_version == manifest.version
        assert req.manifest_hash == manifest.manifest_hash
        assert req.state_epoch == sim.state_epoch == 0
        assert req.created_tick == 0
        assert req.deadline_tick == 10
        assert req.arguments == {"query": "market trends"}
        # input_hash deterministic over the same arguments
        assert req.input_hash == hash_payload({"query": "market trends"})
        # workspace_version is the frozen-view hash (non-"0", real hash)
        assert len(req.workspace_version) == 64

    def test_workspace_version_tracks_committed_writes(self) -> None:
        sim, _, _ = self._submit()
        v0 = sim._build_snapshot(0)["workspace_versions"]["agent.research"]

        # A committed write changes the next tick's frozen view version
        path = f"f-{uuid.uuid4().hex[:8]}.md"
        from my_team.transaction import EffectType
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE, "agent.research", path,
            data={"content": "v1"},
        )
        sim._phase_commit(1, {"agent.research": []})

        v1 = sim._build_snapshot(2)["workspace_versions"]["agent.research"]
        assert v1 != v0
        # No further writes → version stable
        v2 = sim._build_snapshot(3)["workspace_versions"]["agent.research"]
        assert v2 == v1


class TestCompleteTool:
    """registry.complete_tool: correlation + contract recording."""

    def _op_with_request(self) -> tuple[PendingOperationRegistry, Any]:
        reg = PendingOperationRegistry()
        op = reg.submit(
            op_type=OpType.TOOL_REQUEST, agent_id="a", created_tick=0,
        )
        op.tool_request = ToolRequest(
            request_id=op.request_id, agent_id="a", tool_name="t",
            tool_version="1.0.0", manifest_hash="h", input_hash="i",
            state_epoch=0, created_tick=0,
        )
        return reg, op

    def test_complete_tool_records_contract(self) -> None:
        reg, op = self._op_with_request()
        result = ToolResultContract(
            request_id=op.request_id,
            status="completed",
            data={"summary": "ok"},
            output_hash="abc",
            effects={"declared": ["read"], "observed": [], "possible": []},
        )
        reg.complete_tool(op.request_id, result)
        assert op.status == OpStatus.COMPLETED
        assert op.result == {"summary": "ok"}
        assert op.metadata["tool_result"]["output_hash"] == "abc"
        assert op.metadata["tool_result"]["status"] == "completed"

    def test_mismatched_request_id_ignored(self) -> None:
        reg, op = self._op_with_request()
        before = op.status
        reg.complete_tool(op.request_id, ToolResultContract(
            request_id="op.other", data={},
        ))
        assert op.status == before

    def test_late_result_for_cancelled_op_ignored(self) -> None:
        reg, op = self._op_with_request()
        reg.cancel(op.request_id)
        assert op.status == OpStatus.CANCELLED
        reg.complete_tool(op.request_id, ToolResultContract(
            request_id=op.request_id, data={"late": True},
        ))
        assert op.status == OpStatus.CANCELLED
        assert op.result == {}


class TestIngestAuditContract:
    """Delivered results audited with contract fields."""

    def test_tool_result_audit_carries_contract(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        register_remote_tool(sim._tool_registry, "web_search")
        agent = ToolFlowAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        from my_team.models.activation import (
            WakeCondition,
            WakeEventType,
            WakeupEvent,
        )
        cond = sim.scheduler.get_wake_condition("agent.research")
        sim.scheduler.update_wake_condition(
            "agent.research",
            WakeCondition(
                event_types=cond.event_types | {WakeEventType.BOOTSTRAP},
                wake_at_tick=0,
            ),
        )
        sim.scheduler.enqueue_event(WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.research", tick=0,
            source_agent_id="system",
        ))

        sim.run_tick()  # submit
        op = sim._pending_ops.get_by_agent("agent.research")[0]
        manifest = sim._tool_registry.get_manifest("web_search")
        assert manifest is not None

        # Harness completes via the CONTRACT path (complete_tool)
        sim._pending_ops.complete_tool(op.request_id, ToolResultContract(
            request_id=op.request_id,
            status="completed",
            data={"summary": "Market growing"},
            output_hash=hash_payload({"summary": "Market growing"}),
            effects={"declared": [], "observed": [], "possible": ["external_network"]},
        ))
        sim.run_tick()  # ingest delivers + audits

        entries = [
            e for e in sim.audit_log.entries
            if e.event_type == AuditEventType.TOOL_RESULT
            and e.details.get("request_id") == op.request_id
        ]
        assert len(entries) == 1
        d = entries[0].details
        assert d["tool_name"] == "web_search"
        assert d["tool_version"] == manifest.version
        assert d["manifest_hash"] == manifest.manifest_hash
        assert d["input_hash"] == op.tool_request.input_hash
        assert d["output_hash"] == hash_payload({"summary": "Market growing"})
        assert d["result_status"] == "completed"
        assert d["executor_cancel_confirmed"] is False
        # Result still delivered to the agent (contract path does not
        # change delivery semantics)
        assert sim._agent_runtime_states["agent.research"].state == AgentState.IDLE
