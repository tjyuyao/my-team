"""Phase-semantics tests (v0.6.0 review, P0).

Verifies the kernel protocol boundaries (方案 B — Act is an explicit
phase between Validate and Commit):

- Act is in the phase order, between Validate and Commit
- Phase 6 (Validate) is pre-validation: produces NO side effects
- Phase 7 (Act) only stages/registers; it never applies
- Phase 8 (Commit) only applies effects that passed validation
"""

from __future__ import annotations

from my_team.agent_runtime import (
    ActionResult,
    AgentAction,
    AgentObservation,
    BaseAgent,
)
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.activation import ReadyCandidate
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import (
    Intent,
    SubmitToolRequest,
    WritePrivateFileIntent,
)
from my_team.simulation import Simulation
from my_team.transaction import EffectStatus, EffectType

PHASE_ORDER = [
    "ingest", "freeze", "schedule", "observe", "decide",
    "validate", "act", "commit", "publish", "audit",
]


def _make_tree(tools: list[str] | None = None) -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": tools or ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


class UnauthorizedToolAgent(BaseAgent):
    """Emits a SubmitToolRequest for a tool the agent does not have."""

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        return [SubmitToolRequest(
            agent_id=self._agent_id,
            tool_name="web_search",
            arguments={"query": "x"},
        )]


def _ok_result(intent: Intent) -> ActionResult:
    return ActionResult(
        action=AgentAction(
            action_type=intent.intent_type.value,
            tool_name=getattr(intent, "tool_name", ""),
            payload=dict(intent.payload),
        ),
        success=True,
        result_data={"validated": True},
    )


class TestPhaseOrder:
    def test_phase_order_includes_act(self) -> None:
        """Act is an explicit phase, executed between Validate and Commit."""
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        assert sim.last_tick_phases == PHASE_ORDER
        assert "act" in sim.last_tick_phases
        assert sim.last_tick_phases.index("validate") < sim.last_tick_phases.index("act")
        assert sim.last_tick_phases.index("act") < sim.last_tick_phases.index("commit")


class TestValidateNoSideEffects:
    def test_validate_does_not_apply_side_effect(self) -> None:
        """An intent failing pre-validation produces NO side effects:
        no pending op is registered, no wake event, agent not waiting."""
        sim = Simulation(agent_tree=_make_tree(tools=["read", "ls"]))
        agent = UnauthorizedToolAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()

        # Nothing registered or staged
        assert sim._pending_ops.pending_count == 0
        # Agent not left waiting; continuation untouched
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.phase == ContinuationPhase.FRESH
        # Rejection audited
        denied = sim.audit_log.for_event_type(AuditEventType.PERMISSION_DENIED)
        assert len(denied) >= 1


class TestActStagesNotApplies:
    def test_act_only_registers_or_stages_effect(self) -> None:
        """After Act: effects are STAGED, files not written; Commit applies."""
        from uuid import uuid4
        path = f"notes-{uuid4().hex[:8]}.md"
        sim = Simulation(agent_tree=_make_tree())
        intent = WritePrivateFileIntent(
            agent_id="agent.root",
            path=path,
            content="hello",
        )
        plan: dict[str, list[Intent]] = {"agent.root": [intent]}
        validated = {"agent.root": [_ok_result(intent)]}

        results = sim._phase_act(
            0, plan, ready=[], validated=validated,
            snapshot=sim._build_snapshot(0),
        )
        assert results["agent.root"][0].success

        # Staged, NOT applied
        staged = [
            e for e in sim._transaction_buffer.get_effects("agent.root")
            if e.effect_type == EffectType.FILE_WRITE
        ]
        assert len(staged) == 1
        assert staged[0].status == EffectStatus.STAGED
        target = sim._private_store.agent_home("agent.root") / path
        assert not target.exists()

        # Commit is the sole applier
        sim._phase_commit(0, results)
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "hello"


class TestCommitOnlyValidated:
    def test_commit_only_applies_validated_effects(self) -> None:
        """An intent that fails pre-validation never reaches Act/Commit."""
        sim = Simulation(agent_tree=_make_tree(tools=["read", "ls"]))
        intent = SubmitToolRequest(
            agent_id="agent.root",
            tool_name="web_search",
            arguments={"query": "x"},
        )
        plan: dict[str, list[Intent]] = {"agent.root": [intent]}
        candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=0)

        validated = sim._phase_validate(0, plan, ready=[candidate])
        assert not validated["agent.root"][0].success

        results = sim._phase_act(
            0, plan, ready=[], validated=validated,
            snapshot=sim._build_snapshot(0),
        )
        assert not results["agent.root"][0].success

        sim._phase_commit(0, results)
        # Nothing was staged or applied
        assert sim._pending_ops.pending_count == 0
        assert sim._transaction_buffer.get_effects("agent.root") == []
