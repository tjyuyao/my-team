"""State-epoch fencing of external results (v0.6.0 review §四.5).

External operations are stamped with the simulation's state epoch at
submission. Ingest discards results whose epoch no longer matches:

- epoch_mismatch: a rollback/restore invalidated the state the result
  was computed against
- superseded: the agent is no longer waiting for this operation (it
  moved on, e.g. resubmitted after a timeout)

A rollback itself bumps the epoch, so all in-flight results from the
failed tick's era become stale.
"""

from __future__ import annotations

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SubmitLLMRequest
from my_team.simulation import Simulation
from my_team.transaction import EffectType


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


class SubmitAgent(BaseAgent):
    """Agent that submits one async LLM request and waits."""

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]


class TestStaleResponseFencing:
    def test_stale_llm_response_is_ignored(self) -> None:
        """A response submitted under an old state epoch is discarded:
        not delivered to the continuation, no wake, audited as stale."""
        sim = Simulation(agent_tree=_make_tree())
        agent = SubmitAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: agent submits an LLM request under epoch 0
        sim.run_tick()
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1
        assert ops[0].state_epoch == 0

        # State epoch advances (e.g. a rollback happened)
        sim._bump_state_epoch()
        assert sim.state_epoch == 1

        # The provider completes the old-epoch op anyway
        sim._pending_ops.complete(ops[0].request_id, result={"content": "late"})

        # Ingest discards it: no delivery, no wake, removed from registry
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.phase == ContinuationPhase.WAITING_FOR_LLM
        assert rs.continuation.last_llm_result == {}
        assert sim._pending_ops.get_by_agent("agent.root") == []

        stale = sim.audit_log.for_event_type(AuditEventType.STALE_RESULT)
        assert len(stale) == 1
        assert stale[0].details["reason"] == "epoch_mismatch"

    def test_result_for_superseded_operation_is_discarded(self) -> None:
        """A late result for an op the agent no longer waits on is discarded."""
        sim = Simulation(agent_tree=_make_tree())
        agent = SubmitAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()
        old_op = sim._pending_ops.get_by_agent("agent.root")[0]

        # Agent moved on (e.g. timed out and resubmitted): pending now
        # points at a different request
        rs = sim._agent_runtime_states["agent.root"]
        rs.continuation.pending_request_id = "op.superseded"

        # The old op's late result arrives
        sim._pending_ops.complete(old_op.request_id, result={"content": "late"})

        sim.run_tick()
        assert rs.continuation.last_llm_result == {}
        assert sim._pending_ops.get_by_agent("agent.root") == []

        stale = sim.audit_log.for_event_type(AuditEventType.STALE_RESULT)
        assert len(stale) == 1
        assert stale[0].details["reason"] == "superseded"

    def test_rollback_increments_state_epoch(self) -> None:
        """A commit rollback bumps the state epoch, invalidating in-flight
        results from the failed tick's era."""
        sim = Simulation(agent_tree=_make_tree())
        assert sim.state_epoch == 0

        # Force a KERNEL failure (duplicate task ids are DETERMINISTIC
        # failures since T18 — they no longer roll back): a FILE_WRITE
        # whose target path is a directory raises IsADirectoryError.
        from uuid import uuid4
        boom = f"boom-{uuid4().hex[:8]}"
        home = sim._private_store.agent_home("agent.root")
        (home / boom).mkdir(parents=True, exist_ok=True)
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE,
            "agent.root",
            boom,
            data={"content": "boom"},
        )

        sim._phase_commit(0, {})

        assert sim.state_epoch == 1
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert rollbacks[0].details["new_state_epoch"] == 1
