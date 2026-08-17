"""Cross-restart continuation: pending ops + outbox survive save/load.

v0.8.0 P1-1/2 — durable closed loop:

  Pending ops:  agent submits a remote tool request → WAITING_FOR_TOOL
                 save mid-flight → load (fresh process state)
                 external executor completes the op after restart
                 Ingest delivers the result (fenced against the
                 restored state_epoch)

  Outbox:        COMMITTED (never dispatched) entries survive restart
                 and are dispatched by the next tick's commit — the
                 dispatch loop runs every commit, not only when a new
                 email effect is applied; idempotency keys dedupe
                 across restart.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.fake_llm import FakeToolExecutor
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SendEmailIntent, SubmitToolRequest
from my_team.outbox import OutboxStatus
from my_team.pending_ops import OpStatus, OpType
from my_team.simulation import Simulation
from tests.tool_helpers import register_remote_tool


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


def _make_tree_single() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write", "ls", "delegate", "send_email"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


class ToolFlowAgent(BaseAgent):
    """Scripted agent: submit web_search, then report via email."""

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
            summary = result.get("summary", result.get("error", "done"))
            return [
                SendEmailIntent(
                    agent_id=self._agent_id,
                    to=["agent.research"],
                    subject="[RESULT] Search Complete",
                    body=f"Search result: {summary}",
                ),
            ]
        return [
            SubmitToolRequest(
                agent_id=self._agent_id,
                tool_name="web_search",
                arguments={"query": "market trends"},
                timeout_ticks=10,
            ),
        ]


def _setup_tool_flow() -> tuple[Simulation, FakeToolExecutor, ToolFlowAgent]:
    sim = Simulation(agent_tree=_make_tree())
    register_remote_tool(sim, "web_search")
    executor = FakeToolExecutor(latency_ticks=1)
    executor.register_result("agent.research", "web_search", [
        {"success": True, "summary": "Market growing 10% YoY"},
    ])
    agent = ToolFlowAgent("agent.research")
    agent._tool_registry = sim._tool_registry
    sim._runtimes["agent.research"] = agent
    _bootstrap_agent(sim, "agent.research")
    return sim, executor, agent


class TestPendingOpRestartContinuation:
    """SUBMITTED/PENDING ops complete after restart and deliver."""

    def test_inflight_op_survives_restart_and_delivers(self, tmp_path: Path) -> None:
        """Save mid-flight → load → external complete → result delivered."""
        db = tmp_path / f"sim-{uuid.uuid4().hex[:8]}.db"
        sim, executor, agent = _setup_tool_flow()

        # Tick 0: agent submits the tool request → WAITING_FOR_TOOL
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.research"]
        assert rs.state == AgentState.WAITING_FOR_TOOL
        assert sim._pending_ops.pending_count == 1

        # Save while the op is in flight
        sim.save_to(db)
        op_before = sim._pending_ops.get_by_agent("agent.research")[0]

        # "Restart": fresh process state, same DB
        sim2 = Simulation.load_from(db)
        rs2 = sim2._agent_runtime_states["agent.research"]

        # Runtime logic is not persisted — re-install (documented load
        # contract), exactly as for a fresh simulation.
        agent2 = ToolFlowAgent("agent.research")
        agent2._tool_registry = sim2._tool_registry
        sim2._runtimes["agent.research"] = agent2

        # Wait state + op restored with request identity + epoch.
        # Status is SUBMITTED or PENDING — dispatch claimed the op at
        # the submitting tick's publish; both survive restart.
        assert rs2.state == AgentState.WAITING_FOR_TOOL
        op = sim2._pending_ops.get_by_agent("agent.research")[0]
        assert op.request_id == op_before.request_id
        assert op.op_type == OpType.TOOL_REQUEST
        assert op.metadata.get("tool_name") == "web_search"
        assert op.status in {OpStatus.SUBMITTED, OpStatus.PENDING}
        assert op.state_epoch == sim2.state_epoch == sim.state_epoch

        # External executor completes the op AFTER restart
        completed = executor.advance(sim2, current_tick=1)
        assert completed == 1

        # Ingest delivers → agent acts on the result → report email
        sim2.run_tick()
        assert len(sim2._mail_system._all_emails) == 1
        email = list(sim2._mail_system._all_emails.values())[0]
        assert email.subject == "[RESULT] Search Complete"
        assert "Market growing" in email.body
        rs2 = sim2._agent_runtime_states["agent.research"]
        assert rs2.state == AgentState.IDLE

    def test_epoch_fencing_across_restart(self, tmp_path: Path) -> None:
        """A result for the pre-restart epoch is fenced after a new epoch.

        The state the op was computed against no longer exists — the
        result must be discarded (STALE_RESULT / epoch_mismatch), not
        delivered.
        """
        db = tmp_path / f"sim-{uuid.uuid4().hex[:8]}.db"
        sim, executor, agent = _setup_tool_flow()
        sim.run_tick()
        sim.save_to(db)

        sim2 = Simulation.load_from(db)
        agent2 = ToolFlowAgent("agent.research")
        agent2._tool_registry = sim2._tool_registry
        sim2._runtimes["agent.research"] = agent2

        # The world moved on: state epoch advanced (rollback/restore)
        # after restart — the in-flight op belongs to the old epoch.
        epoch_before = sim2.state_epoch
        sim2._bump_state_epoch()

        executor.advance(sim2, current_tick=1)
        sim2.run_tick()

        # Result fenced: discarded, agent NOT woken with it
        assert sim2._pending_ops.get_by_agent("agent.research") == []
        assert sim2._mail_system._all_emails == {}
        stale = [
            e for e in sim2.audit_log.entries
            if e.event_type == AuditEventType.STALE_RESULT
            and e.details.get("reason") == "epoch_mismatch"
        ]
        assert len(stale) == 1
        assert stale[0].details["op_epoch"] == epoch_before
        rs2 = sim2._agent_runtime_states["agent.research"]
        assert rs2.state == AgentState.WAITING_FOR_TOOL  # never delivered

    def test_submitted_op_fields_restored(self, tmp_path: Path) -> None:
        """request_id / tool metadata / epoch / tick survive the roundtrip."""
        db = tmp_path / f"sim-{uuid.uuid4().hex[:8]}.db"
        sim, executor, agent = _setup_tool_flow()
        sim.run_tick()
        op_before = sim._pending_ops.get_by_agent("agent.research")[0]

        sim.save_to(db)
        sim2 = Simulation.load_from(db)
        op = sim2._pending_ops.get_by_agent("agent.research")[0]

        assert op.request_id == op_before.request_id
        assert op.created_tick == op_before.created_tick == 0
        assert op.eligible_tick == 1
        assert op.deadline_tick == op_before.deadline_tick
        assert op.agent_id == "agent.research"
        assert op.state_epoch == 0


class TestOutboxRestartContinuation:
    """COMMITTED outbox entries survive restart and dispatch resumes."""

    def test_committed_entry_dispatched_after_restart(self, tmp_path: Path) -> None:
        """A COMMITTED (never dispatched) entry is delivered by the next
        tick's commit — dispatch runs every commit, not only when a new
        email effect is applied."""
        db = tmp_path / f"sim-{uuid.uuid4().hex[:8]}.db"
        sim = Simulation(agent_tree=_make_tree_single())
        entry = sim._outbox.stage(
            from_agent="agent.root",
            to=["agent.research"],
            subject="Survivor",
            body="dispatched after restart",
            idempotency_key=f"k.{uuid.uuid4().hex[:8]}",
        )
        sim._outbox.commit(entry.entry_id)
        assert sim._outbox.pending_count == 1
        sim.save_to(db)

        sim2 = Simulation.load_from(db)
        restored = sim2._outbox.get(entry.entry_id)
        assert restored is not None
        assert restored.status == OutboxStatus.COMMITTED
        assert restored.subject == "Survivor"

        # Next tick's commit runs the dispatch loop unconditionally
        sim2.run_tick()
        assert sim2._outbox.get(entry.entry_id).status == OutboxStatus.DISPATCHED
        assert len(sim2._mail_system._all_emails) == 1
        email = list(sim2._mail_system._all_emails.values())[0]
        assert email.subject == "Survivor"
        assert sim2._outbox.pending_count == 0

    def test_idempotency_key_survives_restart(self, tmp_path: Path) -> None:
        """Staging the same email across a restart dedupes to one entry."""
        db = tmp_path / f"sim-{uuid.uuid4().hex[:8]}.db"
        sim = Simulation(agent_tree=_make_tree_single())
        sim._outbox.stage(
            from_agent="agent.root", to=["agent.research"],
            subject="Dedupe", idempotency_key="k.dedupe",
        )
        sim.save_to(db)

        sim2 = Simulation.load_from(db)
        again = sim2._outbox.stage(
            from_agent="agent.root", to=["agent.research"],
            subject="Dedupe", idempotency_key="k.dedupe",
        )
        assert sim2._outbox.summary()["total"] == 1
        assert again.idempotency_key == "k.dedupe"
