"""Executor Admission + tiering + request idempotency (v0.8.0 P1-4/5/6).

- Act routes by execution class: PURE/READ_ONLY/STAGED_MUTATION tools
  are kernel-executed (apply_patch stages FILE_PATCH via the intent
  path); LOCAL_PROCESS tools become pending ops.
- Phase 9 dispatch: SUBMITTED → Admission (executor registered, tier
  compatible, capacity) → TRUSTED_IN_PROCESS runs the tool in-process /
  out-of-process executors claim the op.
- Admission denial completes the op with a structured error so Ingest
  wakes the agent.
- request_id history persists across restart: a replayed id is
  rejected (DUPLICATE_REQUEST_ID) — no double charging / side effects.
"""

from __future__ import annotations

import uuid

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.executor_registry import ExecutorTier
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import (
    Intent,
    SendEmailIntent,
    SubmitToolRequest,
)
from my_team.pending_ops import OpStatus, OpType
from my_team.simulation import Simulation
from my_team.tool_manifest import ExecutionClass, ToolManifest
from tests.tool_helpers import register_remote_tool


def _make_tree(tools: list[str] | None = None) -> AgentTree:
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
                "tools": ["read", "write", "ls", "send_email"]
                        + (tools or []),
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _bootstrap(sim: Simulation, agent_id: str) -> None:
    from my_team.models.activation import WakeCondition, WakeEventType, WakeupEvent
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


class ScriptedToolAgent(BaseAgent):
    """Submits ONE tool request per activation, then reports via email."""

    tool_name = "web_search"
    arguments: dict = {"query": "x"}
    request_id: str | None = None

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
            r = continuation.last_tool_result
            return [SendEmailIntent(
                agent_id=self._agent_id,
                to=["agent.research"],
                subject="[TOOL DONE]",
                body=str(r.get("error", r.get("summary", "ok"))),
            )]
        return [SubmitToolRequest(
            agent_id=self._agent_id,
            request_id=self.request_id or f"tool.req.{uuid.uuid4().hex[:8]}",
            tool_name=self.tool_name,
            arguments=self.arguments,
            timeout_ticks=10,
        )]


class ReplayAgent(ScriptedToolAgent):
    """Submits with a FIXED request_id (replay scenario)."""

    request_id = "tool.req.replay.fixed"


class TestManifestRoutingAtAct:
    """Act routes by execution class, not a hardcoded name list."""

    def test_apply_patch_intent_is_kernel_executed(self) -> None:
        """STAGED_MUTATION tools stage effects at Act (no pending op)."""
        sim = Simulation(agent_tree=_make_tree(["apply_patch"]))
        agent = ScriptedToolAgent("agent.research")
        agent.tool_name = "apply_patch"
        agent.arguments = {"path": f"f-{uuid.uuid4().hex[:8]}.md", "patch": ""}
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        sim.run_tick()

        # No pending op — the tool ran at Act
        assert sim._pending_ops.get_by_agent("agent.research") == []
        # FILE_PATCH was NOT staged for an empty patch (invalid) — the
        # intent path at least routed to the handler; verify with a
        # valid patch instead via the registry result shape below.
        dispatched = [
            e for e in sim.audit_log.entries
            if e.event_type == AuditEventType.TOOL_DISPATCHED
        ]
        assert dispatched == []

    def test_apply_patch_valid_patch_stages_at_act(self) -> None:
        """A valid patch via the INTENT path stages FILE_PATCH."""
        sim = Simulation(agent_tree=_make_tree(["apply_patch"]))
        path = f"p-{uuid.uuid4().hex[:8]}.md"
        target = sim._private_store.agent_home("agent.research") / path
        target.write_text("hello", encoding="utf-8")
        patch = (
            "--- a/" + path + "\n+++ b/" + path + "\n"
            "@@ -1,1 +1,1 @@\n"
            "-hello\n"
            "+goodbye\n"
        )
        agent = ScriptedToolAgent("agent.research")
        agent.tool_name = "apply_patch"
        agent.arguments = {"path": path, "patch": patch}
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        sim.run_tick()

        # Staged at Act as FILE_PATCH, applied at Commit — the file is
        # patched and the buffer was cleared at tick end.
        assert target.read_text(encoding="utf-8") == "goodbye"

    def test_run_tests_intent_dispatches_to_trusted_executor(self) -> None:
        """LOCAL_PROCESS tools: pending op → dispatch executes for real."""
        sim = Simulation(agent_tree=_make_tree(["run_tests"]))
        agent = ScriptedToolAgent("agent.research")
        agent.tool_name = "run_tests"
        agent.arguments = {"test_path": "tests/test_outbox.py"}
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        sim.run_tick()

        # Op went through dispatch → executed in-process → completed
        op = sim._pending_ops.get_by_agent("agent.research")[0]
        assert op.op_type == OpType.TOOL_REQUEST
        assert op.status == OpStatus.COMPLETED
        assert op.result.get("exit_code") == 0
        assert op.result.get("success") is True
        # Dispatched audit records the tier
        dispatched = [
            e for e in sim.audit_log.entries
            if e.event_type == AuditEventType.TOOL_DISPATCHED
            and e.details.get("request_id") == op.request_id
        ]
        assert len(dispatched) == 1
        assert dispatched[0].details["status"] == "executed"
        assert dispatched[0].details["executor_tier"] == "trusted_in_process"

        # Result delivered next tick (agent reports via email)
        sim.run_tick()
        assert len(sim._mail_system._all_emails) == 1


class TestExecutorAdmission:
    """Admission: executor registered, tier compatible, capacity."""

    def test_no_executor_admission_denied(self) -> None:
        """A tool without a registered executor fails with a structured
        error — the agent is woken, not left waiting forever."""
        sim = Simulation(agent_tree=_make_tree(["web_search"]))
        sim._tool_registry.register_handler(
            "web_search", lambda **_: None,
            manifest=ToolManifest(
                name="web_search", version="1.0.0",
                execution_class=ExecutionClass.EXTERNAL_IRREVERSIBLE,
                reversible=False,
            ),
        )
        # NOTE: no executor registered for web_search
        agent = ScriptedToolAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        sim.run_tick()
        op = sim._pending_ops.get_by_agent("agent.research")[0]
        assert op.status == OpStatus.COMPLETED
        assert op.result.get("error_code") == "admission_denied"
        denied = [
            e for e in sim.audit_log.entries
            if e.event_type == AuditEventType.TOOL_DISPATCHED
            and e.details.get("status") == "admission_denied"
        ]
        assert len(denied) == 1
        assert "No executor registered" in denied[0].error

        # Agent woken with the structured error → reports it
        sim.run_tick()
        assert len(sim._mail_system._all_emails) == 1
        email = list(sim._mail_system._all_emails.values())[0]
        assert "No executor registered" in email.body

    def test_tier_mismatch_denied(self) -> None:
        """A SANDBOXED_PROCESS manifest refuses a TRUSTED_IN_PROCESS
        executor; only SANDBOXED_OUT_OF_PROCESS admits it."""
        sim = Simulation(agent_tree=_make_tree())
        sim._tool_registry.register_manifest(ToolManifest(
            name="sandbox_tool", version="1.0.0",
            execution_class=ExecutionClass.SANDBOXED_PROCESS,
        ))
        sim._executors.register(
            "sandbox_tool", tier=ExecutorTier.TRUSTED_IN_PROCESS,
        )
        manifest = sim._tool_registry.get_manifest("sandbox_tool")
        admitted, reason, retryable = sim._executors.admit(
            "sandbox_tool", manifest, in_flight=0,
        )
        assert not admitted
        assert "tier" in reason
        assert retryable is False  # permanent, not capacity

        sim._executors.register(
            "sandbox_tool", tier=ExecutorTier.SANDBOXED_OUT_OF_PROCESS,
        )
        admitted, _, _ = sim._executors.admit(
            "sandbox_tool", manifest, in_flight=0,
        )
        assert admitted

    def test_capacity_backpressure(self) -> None:
        """At capacity, ops stay SUBMITTED; re-admitted next tick."""
        sim = Simulation(agent_tree=_make_tree(["web_search"]))
        register_remote_tool(sim, "web_search", max_concurrent=1)
        op1 = sim._pending_ops.submit(
            OpType.TOOL_REQUEST, "agent.research", 0,
            metadata={"tool_name": "web_search", "request_id": "r1"},
        )
        op2 = sim._pending_ops.submit(
            OpType.TOOL_REQUEST, "agent.research", 0,
            metadata={"tool_name": "web_search", "request_id": "r2"},
        )

        sim._phase_dispatch(0)
        assert op1.status == OpStatus.PENDING   # claimed
        assert op2.status == OpStatus.SUBMITTED  # at capacity

        # First completes → next dispatch admits the second
        sim._pending_ops.complete(op1.request_id, result={"success": True})
        sim._phase_dispatch(1)
        assert op2.status == OpStatus.PENDING

    def test_local_process_accepts_trusted_tier(self) -> None:
        """LOCAL_PROCESS manifests admit TRUSTED_IN_PROCESS executors."""
        sim = Simulation(agent_tree=_make_tree())
        manifest = sim._tool_registry.get_manifest("run_tests")
        assert manifest is not None
        admitted, _, _ = sim._executors.admit(
            "run_tests", manifest, in_flight=0,
        )
        assert admitted


class TestRequestIdempotency:
    """request_id history: replay rejected, persisted across restart."""

    def test_replayed_request_id_rejected_after_restart(
        self, tmp_path,
    ) -> None:
        """A fixed request_id that was submitted before the restart is
        rejected as a duplicate — no double charging / side effects."""
        db = tmp_path / f"sim-{uuid.uuid4().hex[:8]}.db"
        sim = Simulation(agent_tree=_make_tree(["web_search"]))
        register_remote_tool(sim, "web_search")
        agent = ReplayAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        sim.run_tick()  # submits with the FIXED request_id
        assert len(sim._pending_ops.get_by_agent("agent.research")) == 1
        sim.save_to(db)

        sim2 = Simulation.load_from(db)
        agent2 = ReplayAgent("agent.research")
        agent2._tool_registry = sim2._tool_registry
        sim2._runtimes["agent.research"] = agent2

        # The replayed request_id is rejected at PreValidate
        from my_team.models.activation import ReadyCandidate
        from my_team.models.intent import SubmitToolRequest as STR
        plan = [STR(
            agent_id="agent.research",
            request_id="tool.req.replay.fixed",
            tool_name="web_search",
            arguments={"query": "replay"},
        )]
        ready = [ReadyCandidate(
            agent_id="agent.research", events=(), tick=1,
        )]
        validated = sim2._phase_validate(1, {"agent.research": plan}, ready)
        vr = validated["agent.research"][0]
        assert not vr.success
        assert vr.error_code == "DUPLICATE_REQUEST_ID"

        # No second op was created
        assert sim2._pending_ops.get_by_agent("agent.research")[0].metadata.get(
            "request_id",
        ) == "tool.req.replay.fixed"

    def test_seen_requests_persisted(self, tmp_path) -> None:
        db = tmp_path / f"sim-{uuid.uuid4().hex[:8]}.db"
        sim = Simulation(agent_tree=_make_tree(["web_search"]))
        register_remote_tool(sim, "web_search")
        agent = ReplayAgent("agent.research")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")
        sim.run_tick()
        sim.save_to(db)

        sim2 = Simulation.load_from(db)
        assert sim2._pending_ops.is_seen(
            "agent.research", "tool.req.replay.fixed",
        )
        # Scoped per agent: another agent reusing the id is NOT "seen"
        assert not sim2._pending_ops.is_seen(
            "agent.other", "tool.req.replay.fixed",
        )

    def test_same_plan_duplicate_rejected(self) -> None:
        """Two intents with the same request_id in one plan → second
        rejected (existing Check 1c)."""
        sim = Simulation(agent_tree=_make_tree(["web_search"]))
        register_remote_tool(sim, "web_search")
        from my_team.models.activation import ReadyCandidate
        from my_team.models.intent import SubmitToolRequest as STR
        plan = [
            STR(agent_id="agent.research", request_id="dup.1",
                tool_name="web_search", arguments={}),
            STR(agent_id="agent.research", request_id="dup.1",
                tool_name="web_search", arguments={}),
        ]
        ready = [ReadyCandidate(
            agent_id="agent.research", events=(), tick=0, visible_at_tick=0,
        )]
        validated = sim._phase_validate(0, {"agent.research": plan}, ready)
        results = validated["agent.research"]
        assert results[0].success
        assert not results[1].success
        assert results[1].error_code == "DUPLICATE_REQUEST_ID"
