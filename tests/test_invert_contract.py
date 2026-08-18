"""T18 (invert contract) + T20 (KB lock integration) edge tests.

Covers the pieces the refactored rollback/commit path must prove:
- Every EffectType declares an invert operation (contract completeness)
- Group atomicity holds for APPLY-TIME deterministic failures (a
  duplicate TASK_CREATE fails its EMAIL_SEND sibling — both FAILED, the
  tick still COMMITS, nothing rolls back)
- T20: same-tick kb_write contention is a deterministic LOCK_CONFLICT;
  the loser retries next tick and succeeds; a rolled-back tick releases
  the handler-acquired lock
"""

from __future__ import annotations

from uuid import uuid4

from my_team.agent_runtime import ToolContext
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.shared_kb import PermissionRule
from my_team.simulation import Simulation
from my_team.transaction import (
    INVERT_CONTRACT,
    EffectStatus,
    EffectType,
)


def _two_agent_sim() -> Simulation:
    """Two agents, both with kb_write + permission on the same scope."""
    tree = AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate", "kb_write"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "kb_write"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })
    sim = Simulation(agent_tree=tree)
    sim._permission_engine.add_rules([
        PermissionRule(
            scope="project/research/*",
            principal="agent.root",
            allow=["read", "create", "write", "kb_write", "lock", "unlock"],
        ),
        PermissionRule(
            scope="project/research/*",
            principal="agent.research",
            allow=["read", "create", "write", "kb_write", "lock", "unlock"],
        ),
    ])
    return sim


def _ctx(sim: Simulation, agent_id: str, tick: int) -> ToolContext:
    return ToolContext(
        agent_id=agent_id, tick=tick,
        allowed_tools=sim._tool_registry.get_allowed_tools(agent_id),
    )


class TestInvertContract:
    """Every EffectType must declare how it is undone (SPEC §3.3)."""

    def test_every_effect_type_has_invert_definition(self) -> None:
        """The contract covers ALL enum members — no effect type can be
        staged without a declared invert."""
        for etype in EffectType:
            assert etype in INVERT_CONTRACT, (
                f"{etype.value} missing from INVERT_CONTRACT"
            )

    def test_contract_records_prior_value_semantics(self) -> None:
        """Each declaration describes what invert_data records."""
        for etype, spec in INVERT_CONTRACT.items():
            assert spec.kind.value  # enum value present
            assert spec.recorded  # human-readable prior-value semantics

    def test_group_duplicate_task_fails_locally_email_aborted(self) -> None:
        """A duplicate TASK_CREATE inside a 'group' fails the WHOLE
        group at apply time (email sibling FAILED, outbox discarded) —
        and the tick still commits (no rollback)."""
        sim = _two_agent_sim()
        sim.task_tree.create(
            task_id="task.existing", title="Existing",
            creator_agent_id="agent.root", owner_agent_id="agent.research",
        )
        group = f"group.{uuid4().hex[:8]}"
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.existing",  # duplicate → deterministic FAILED at apply
            data={
                "task_id": "task.existing",
                "title": "Dup",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
            group_id=group,
            atomicity="group",
        )
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND,
            "agent.root",
            "email:agent.root",
            data={
                "from_agent": "agent.root",
                "to": ["agent.research"],
                "subject": "must not send",
                "body": "group sibling",
                "email_type": "delegation",
                "task_id": "task.existing",
            },
            group_id=group,
            atomicity="group",
        )
        # Independent effect — must still commit despite the group failure
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND,
            "agent.root",
            "email:agent.root",
            data={
                "from_agent": "agent.root",
                "to": ["agent.research"],
                "subject": "independent email",
                "body": "commits",
            },
        )

        sim._phase_commit(0, {})

        all_effects = list(sim._transaction_buffer._effects.values())
        create_effect = next(
            e for e in all_effects if e.effect_type == EffectType.TASK_CREATE
        )
        group_email = next(
            e for e in all_effects
            if e.effect_type == EffectType.EMAIL_SEND and e.group_id == group
        )
        independent_email = next(
            e for e in all_effects
            if e.effect_type == EffectType.EMAIL_SEND and e.group_id != group
        )
        assert create_effect.status == EffectStatus.FAILED
        assert "already exists" in (create_effect.error or "")
        # Group sibling FAILED with it — no email sent (inverted even if
        # it had already applied)
        assert group_email.status == EffectStatus.FAILED
        assert "group member failed" in (group_email.error or "")
        # Independent email still committed and delivered
        assert independent_email.status == EffectStatus.COMMITTED
        assert len(sim._mail_system._all_emails) == 1
        subjects = [e.subject for e in sim._mail_system._all_emails.values()]
        assert "independent email" in subjects
        # NO tick rollback
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) == 0


class TestKbLockIntegration:
    """T20: write = auto lock; contention is deterministic; rollback
    releases this tick's locks."""

    RESOURCE = "project/research/report.md"

    def test_same_tick_contention_winner_commits_loser_retries(self) -> None:
        """agent.root acquires first (writes); agent.research gets a
        deterministic LOCK_CONFLICT and succeeds on retry next tick."""
        sim = _two_agent_sim()

        # Tick 0: both agents write the same resource
        r1 = sim._tool_registry.execute(
            _ctx(sim, "agent.root", 0), "kb_write",
            path=self.RESOURCE, content="root version", expected_version=0,
        )
        r2 = sim._tool_registry.execute(
            _ctx(sim, "agent.research", 0), "kb_write",
            path=self.RESOURCE, content="research version", expected_version=0,
        )
        assert r1.success
        assert not r2.success
        assert r2.error_code == "LOCK_CONFLICT"
        assert r2.retryable is True

        sim._phase_commit(0, {})
        assert sim._shared_kb.read(self.RESOURCE, "agent.root").content == (
            "root version"
        )
        # Winner's lock auto-released at commit end
        assert not sim._lock_manager.is_locked(self.RESOURCE)
        # No tick rollback (LOCK_CONFLICT is a deterministic failure)
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) == 0
        # Lock lifecycle audited
        audit_types = {
            e.event_type
            for e in sim.audit_log.for_event_type(AuditEventType.LOCK_ACQUIRED)
        } | {
            e.event_type
            for e in sim.audit_log.for_event_type(AuditEventType.LOCK_CONFLICT)
        } | {
            e.event_type
            for e in sim.audit_log.for_event_type(AuditEventType.LOCK_RELEASED)
        }
        assert audit_types == {
            AuditEventType.LOCK_ACQUIRED,
            AuditEventType.LOCK_CONFLICT,
            AuditEventType.LOCK_RELEASED,
        }

        # Tick 1: loser retries and succeeds (lock is free — released at
        # commit end, so no lease wait)
        r3 = sim._tool_registry.execute(
            _ctx(sim, "agent.research", 1), "kb_write",
            path=self.RESOURCE, content="research version", expected_version=1,
        )
        assert r3.success
        sim._phase_commit(1, {})
        resource = sim._shared_kb.read(self.RESOURCE, "agent.research")
        assert resource.content == "research version"
        assert resource.version == 2

    def test_rollback_releases_handler_acquired_lock(self) -> None:
        """A lock acquired by the kb_write handler in this tick is
        RELEASED by the rollback when the tick kernel-fails."""
        sim = _two_agent_sim()
        r = sim._tool_registry.execute(
            _ctx(sim, "agent.root", 0), "kb_write",
            path=self.RESOURCE, content="v1", expected_version=0,
        )
        assert r.success
        assert sim._lock_manager.is_locked(self.RESOURCE)

        # Kernel failure in the same tick: FILE_WRITE to a directory path
        boom = f"boom-{uuid4().hex[:8]}"
        home = sim._private_store.agent_home("agent.root")
        (home / boom).mkdir(parents=True, exist_ok=True)
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE, "agent.root", boom,
            data={"content": "boom"},
        )
        sim._phase_commit(0, {})

        # Rollback released the handler-acquired lock — no lease-stall
        assert not sim._lock_manager.is_locked(self.RESOURCE)
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) == 1
