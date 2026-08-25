"""Cross-process recovery (v0.8.0 P2-9).

- Worker crash → op FAILED → Ingest wakes the agent with a structured
  error → the agent retries with a fresh request_id (no double
  effects — the old id is fenced by the seen-request history)
- Multiple save/load cycles: state and audit converge, no duplicate
  deliveries, tick engine continuity
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
from my_team.pending_ops import OpStatus
from my_team.simulation import Simulation
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


class RetryAgent(BaseAgent):
    """Retries once on failure, then gives up; emails on success."""

    def __init__(self, agent_id: str, **kwargs: object) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self.retried = False

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
            if r.get("failed") and not self.retried:
                self.retried = True
                return [SubmitToolRequest(
                    agent_id=self._agent_id,
                    tool_name="web_search",
                    arguments={"query": "retry"},
                    timeout_ticks=10,
                )]
            return [SendEmailIntent(
                agent_id=self._agent_id,
                to=["agent.research"],
                subject="[DONE]",
                body=str(r.get("summary", r.get("error", "done"))),
            )]
        return [SubmitToolRequest(
            agent_id=self._agent_id,
            tool_name="web_search",
            arguments={"query": "market trends"},
            timeout_ticks=10,
        )]


def _setup() -> tuple[Simulation, FakeToolExecutor, RetryAgent]:
    sim = Simulation(agent_tree=_make_tree())
    register_remote_tool(sim, "web_search")
    executor = FakeToolExecutor(latency_ticks=1)
    executor.register_result("agent.research", "web_search", [
        {"success": True, "summary": "Recovered"},
    ])
    agent = RetryAgent("agent.research")
    agent._tool_registry = sim._tool_registry
    sim._runtimes["agent.research"] = agent
    _bootstrap(sim, "agent.research")
    return sim, executor, agent


class TestWorkerCrashRecovery:
    """Crash → FAILED → structured wake → retry."""

    def test_failed_op_wakes_agent_with_error(self) -> None:
        sim, executor, agent = _setup()
        sim.run_tick()  # submit
        op = sim._pending_ops.get_by_agent("agent.research")[0]
        assert op.status == OpStatus.PENDING

        # The worker dies mid-flight
        sim._pending_ops.fail(op.request_id, "worker crashed (exit 137)")

        sim.run_tick()  # Ingest wakes the agent with the failure
        # The FAILED op is consumed; the agent was woken (it processed
        # the failure and immediately retried with a fresh request)
        ops = sim._pending_ops.get_by_agent("agent.research")
        assert len(ops) == 1
        assert ops[0].request_id != op.request_id

        # Failure audited
        entries = [
            e for e in sim.audit_log.entries
            if e.event_type == AuditEventType.TOOL_RESULT
            and e.details.get("request_id") == op.request_id
            and e.details.get("status") == "failed"
        ]
        assert len(entries) == 1
        assert "worker crashed" in entries[0].error

    def test_agent_retries_with_fresh_request_id(self) -> None:
        sim, executor, agent = _setup()
        sim.run_tick()
        op1 = sim._pending_ops.get_by_agent("agent.research")[0]
        sim._pending_ops.fail(op1.request_id, "worker crashed")

        # Retry tick: agent re-submits with a NEW request_id
        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.research")
        assert len(ops) == 1
        op2 = ops[0]
        assert op2.request_id != op1.request_id
        # Old request_id is fenced by the seen-request history — a
        # replay of op1's id would be rejected
        assert sim._pending_ops.is_seen(
            "agent.research", op1.metadata["request_id"],
        )

        # The retried op completes normally → agent reports success
        completed = executor.advance(sim, current_tick=2)
        assert completed == 1
        sim.run_tick()
        assert len(sim._mail_system._all_emails) == 1
        email = list(sim._mail_system._all_emails.values())[0]
        assert email.subject == "[DONE]"
        assert "Recovered" in email.body


class TestMultiRestartConvergence:
    """Several save/load cycles: state + audit converge."""

    def test_three_restarts_state_and_audit_converge(
        self, tmp_path: Path,
    ) -> None:
        db = tmp_path / f"sim-{uuid.uuid4().hex[:8]}.db"
        sim, executor, agent = _setup()
        sim.run_tick()  # tick 0: submit
        sim.save_to(db)

        # Restart 1: executor completes after restart, result delivered
        sim2 = Simulation.load_from(db)
        agent2 = RetryAgent("agent.research")
        agent2._tool_registry = sim2._tool_registry
        sim2._runtimes["agent.research"] = agent2
        executor.advance(sim2, current_tick=1)
        sim2.run_tick()  # tick 1: deliver → email
        assert len(sim2._mail_system._all_emails) == 1
        audit_len_1 = len(sim2.audit_log.entries)
        sim2.save_to(db)

        # Restart 2: idle tick, then save
        sim3 = Simulation.load_from(db)
        assert sim3._tick_engine.current_tick == 2
        assert len(sim3._mail_system._all_emails) == 1
        sim3.run_tick()  # tick 2: agent idle (no new events)
        audit_len_2 = len(sim3.audit_log.entries)
        assert audit_len_2 > audit_len_1  # TICK_COMPLETE recorded
        sim3.save_to(db)

        # Restart 3: final state
        sim4 = Simulation.load_from(db)
        assert sim4._tick_engine.current_tick == 3
        assert len(sim4._mail_system._all_emails) == 1  # no duplicates
        assert len(sim4.audit_log.entries) == audit_len_2
        # One and only one result email ever delivered
        subjects = [
            e.subject for e in sim4._mail_system._all_emails.values()
        ]
        assert subjects.count("[DONE]") == 1
        rs4 = sim4._agent_runtime_states["agent.research"]
        assert rs4.state == AgentState.IDLE
