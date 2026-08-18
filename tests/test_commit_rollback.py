"""Tests for cross-effect commit rollback and failure grading (T18).

Verifies the two-tier failure semantics (user-approved 2026-08-18):

  Deterministic (business) failure — duplicate task_id, stale patch,
  lock/version/permission — FAILS the effect locally (EffectStatus.
  FAILED); the rest of the tick still COMMITS, NO tick rollback.

  Kernel failure — an unexpected exception during apply (e.g. write to
  a path occupied by a directory → IsADirectoryError) — triggers the
  full-tick rollback: previously applied effects are inverted via their
  declared invert operations (SPEC §3.3 回滚=逆操作), the outbox is
  discarded, and the state epoch is bumped.

To force a genuine kernel failure the tests stage a FILE_WRITE whose
target path is occupied by a DIRECTORY — the apply raises
IsADirectoryError, which is not a pre-checked deterministic condition.
"""

from __future__ import annotations

from uuid import uuid4

from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.activation import WakeEventType
from my_team.shared_kb import PermissionRule
from my_team.simulation import Simulation
from my_team.transaction import EffectStatus, EffectType


def _make_sim() -> Simulation:
    tree = AgentTree.from_dict({
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
    return Simulation(agent_tree=tree)


def _stage_kernel_boom(sim: Simulation) -> None:
    """Stage a FILE_WRITE whose apply raises IsADirectoryError — a
    genuine kernel-level failure (unexpected exception) that triggers
    the full-tick rollback (T18)."""
    boom = f"boom-{uuid4().hex[:8]}"
    home = sim._private_store.agent_home("agent.root")
    (home / boom).mkdir(parents=True, exist_ok=True)
    sim._transaction_buffer.stage(
        EffectType.FILE_WRITE, "agent.root", boom,
        data={"content": "boom"},
    )


class TestDeterministicFailureStaysLocal:
    """T18: business failures FAIL locally — no tick rollback."""

    def test_task_create_duplicate_fails_locally_email_still_commits(
        self,
    ) -> None:
        """Duplicate TASK_CREATE is a DETERMINISTIC failure: the email
        (independent effect) still commits; no rollback happens."""
        sim = _make_sim()
        # Pre-create a task with the id that will be staged
        sim.task_tree.create(
            task_id="task.duplicate",
            title="Existing",
            creator_agent_id="agent.root",
            owner_agent_id="agent.research",
        )

        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND,
            "agent.root",
            "email:agent.root",
            data={
                "from_agent": "agent.root",
                "to": ["agent.research"],
                "subject": "Kept email",
                "body": "This email commits despite the dup failure",
            },
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.duplicate",  # already exists → deterministic FAILED
            data={
                "task_id": "task.duplicate",
                "title": "Duplicate",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )

        sim._phase_commit(0, {})

        # Email committed and delivered — NOT rolled back
        assert len(sim._mail_system._all_emails) == 1
        subjects = [e.subject for e in sim._mail_system._all_emails.values()]
        assert "Kept email" in subjects

        # TASK_CREATE failed locally with a deterministic error
        effects = list(sim._transaction_buffer._effects.values())
        create_effect = next(
            e for e in effects if e.effect_type == EffectType.TASK_CREATE
        )
        assert create_effect.status == EffectStatus.FAILED
        assert "already exists" in (create_effect.error or "")

        # NO rollback audit events
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) == 0

    def test_successful_commit_no_rollback(self) -> None:
        """When all effects apply, no rollback happens."""
        sim = _make_sim()

        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.ok",
            data={
                "task_id": "task.ok",
                "title": "OK",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND,
            "agent.root",
            "email:agent.root",
            data={
                "from_agent": "agent.root",
                "to": ["agent.research"],
                "subject": "Kept",
                "body": "This email stays",
            },
        )

        sim._phase_commit(0, {})

        # Task and email both committed
        assert sim.task_tree.exists("task.ok")
        assert len(sim._mail_system._all_emails) == 1

        # No rollback audit events
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) == 0

    def test_duplicate_task_fails_locally_preserves_prior_state(
        self,
    ) -> None:
        """A deterministic failure touches neither prior state nor the
        tick's other effects."""
        sim = _make_sim()
        # Prior state: a task + email exist from a previous tick
        sim.task_tree.create(
            task_id="task.prior",
            title="Prior",
            creator_agent_id="agent.root",
            owner_agent_id="agent.research",
        )
        sim.mail_system.create_email(
            from_agent="agent.root",
            to=["agent.research"],
            subject="Prior email",
            body="exists",
            tick=0,
        )
        prior_emails = len(sim._mail_system._all_emails)

        # This tick: EMAIL_SEND then a DUPLICATE TASK_CREATE (business
        # failure — stays local).
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND,
            "agent.root",
            "email:agent.root",
            data={
                "from_agent": "agent.root",
                "to": ["agent.research"],
                "subject": "New email",
                "body": "still commits",
            },
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.prior",  # duplicate of pre-existing → deterministic FAILED
            data={
                "task_id": "task.prior",
                "title": "Duplicate",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )

        sim._phase_commit(0, {})

        # Prior state intact; the new email COMMITS (no rollback)
        assert sim.task_tree.exists("task.prior")
        assert len(sim._mail_system._all_emails) == prior_emails + 1
        subjects = [e.subject for e in sim._mail_system._all_emails.values()]
        assert "New email" in subjects

        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) == 0


class TestCommitRollback:
    """Cross-effect rollback when a KERNEL failure occurs during apply."""

    def test_rollback_removes_created_task(self) -> None:
        """TASK_CREATE applied first, then kernel failure → the created
        task is removed (REMOVE_CREATED invert)."""
        sim = _make_sim()
        # Pre-existing task that must survive rollback untouched
        sim.task_tree.create(
            task_id="task.conflict",
            title="Existing",
            creator_agent_id="agent.root",
            owner_agent_id="agent.research",
        )

        # Order: valid TASK_CREATE first, then a kernel-boom FILE_WRITE
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.first",
            data={
                "task_id": "task.first",
                "title": "First",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )
        _stage_kernel_boom(sim)

        sim._phase_commit(0, {})

        # First task rolled back — only the pre-existing one remains
        assert not sim.task_tree.exists("task.first")
        assert sim.task_tree.exists("task.conflict")

    def test_rolled_back_email_produces_no_wake_event(self) -> None:
        """A rolled-back email never generates a NEW_EMAIL wake event."""
        sim = _make_sim()

        # Stage EMAIL_SEND then a kernel-boom FILE_WRITE
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND,
            "agent.root",
            "email:agent.root",
            data={
                "from_agent": "agent.root",
                "to": ["agent.research"],
                "subject": "Doomed email",
                "body": "will roll back",
            },
        )
        _stage_kernel_boom(sim)

        sim._phase_commit(0, {})
        assert len(sim._mail_system._all_emails) == 0

        # The email was never delivered, so no NEW_EMAIL event exists —
        # and a subsequent tick delivers nothing / wakes nobody
        new_email_events = [
            qe for qe in sim._scheduler._events
            if qe.event.event_type == WakeEventType.NEW_EMAIL
        ]
        assert new_email_events == []
        sim.run_tick()
        assert len(sim._mail_system._all_emails) == 0


class TestRollbackCompleteness:
    """Rollback covers file/KB state and leaves no orphan wake events."""

    def test_kb_write_rollback_restores_previous_version(self) -> None:
        """A KB_WRITE applied in a tick that rolls back is undone:
        previous content and version restored."""
        sim = _make_sim()
        sim._permission_engine.add_rules([
            PermissionRule(
                scope="project/research/*",
                principal="agent.root",
                allow=["read", "create", "write", "kb_write", "lock", "unlock"],
            ),
        ])
        # Prior state: KB resource at v1
        sim._shared_kb.create(
            path="project/research/notes.md",
            agent_id="agent.root",
            content="v1",
            tick=0,
        )
        assert sim._shared_kb.versions.get_version("project/research/notes.md") == 1

        # This tick: KB_WRITE (v1 → v2), then a kernel failure
        lock = sim._lock_manager.acquire(
            "project/research/notes.md", "agent.root", current_tick=1,
        )
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.root",
            "project/research/notes.md",
            data={"content": "v2", "expected_version": 1},
            expected_version=1,
            lock_token=lock.lock_token,
        )
        _stage_kernel_boom(sim)

        sim._phase_commit(1, {})

        # KB restored: content and version back to v1
        resource = sim._shared_kb.read("project/research/notes.md", "agent.root")
        assert resource.content == "v1"
        assert resource.version == 1
        assert sim._shared_kb.versions.get_version("project/research/notes.md") == 1
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) >= 1
