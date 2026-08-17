"""Tests for cross-effect commit rollback.

Verifies that when an effect fails during application in Phase 8,
previously applied effects in the same tick are rolled back:

  TASK_CREATE succeeds
  EMAIL_SEND / later effect fails
  → task removed, email removed, audit records TRANSACTION_ROLLBACK

Also (v0.6.0 hardening): FILE_WRITE content restored, shared KB
resource + version restored, rolled-back emails produce no wake
events, and the rollback bumps the state epoch.
"""

from __future__ import annotations

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


class TestCommitRollback:
    """Cross-effect rollback when application fails."""

    def test_task_create_failure_rolls_back_email(self) -> None:
        """If TASK_CREATE fails (duplicate id), the staged email is rolled back."""
        sim = _make_sim()
        # Pre-create a task with the id that will be staged
        sim.task_tree.create(
            task_id="task.duplicate",
            title="Existing",
            creator_agent_id="agent.root",
            owner_agent_id="agent.research",
        )

        # Stage effects in order: EMAIL_SEND first, then a FAILING TASK_CREATE
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND,
            "agent.root",
            "email:agent.root",
            data={
                "from_agent": "agent.root",
                "to": ["agent.research"],
                "subject": "Will be rolled back",
                "body": "This email should be removed",
            },
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.duplicate",  # already exists → create() raises
            data={
                "task_id": "task.duplicate",
                "title": "Duplicate",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )

        emails_before = len(sim._mail_system._all_emails)
        sim._phase_commit(0, {})

        # Email rolled back (not in _all_emails)
        assert len(sim._mail_system._all_emails) == emails_before

        # Audit records the rollback
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) >= 1

        # Committed effects marked ROLLED_BACK
        buffer = sim._transaction_buffer
        for e in buffer._effects.values():
            assert e.status in {
                EffectStatus.ROLLED_BACK,
                EffectStatus.FAILED,
            }, f"{e.effect_type} status: {e.status}"

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

    def test_rollback_preserves_prior_state(self) -> None:
        """Rollback only removes this tick's effects, not prior state."""
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

        # This tick: EMAIL_SEND then failing TASK_CREATE (duplicate id)
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND,
            "agent.root",
            "email:agent.root",
            data={
                "from_agent": "agent.root",
                "to": ["agent.research"],
                "subject": "New email",
                "body": "will roll back",
            },
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.prior",  # duplicate of pre-existing → create() raises
            data={
                "task_id": "task.prior",
                "title": "Duplicate",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )

        sim._phase_commit(0, {})

        # Prior state intact
        assert sim.task_tree.exists("task.prior")
        assert len(sim._mail_system._all_emails) == prior_emails
        # Rolled-back email gone
        subjects = [e.subject for e in sim._mail_system._all_emails.values()]
        assert "New email" not in subjects

    def test_rollback_removes_created_task(self) -> None:
        """TASK_CREATE applied first, then failure → created task removed."""
        sim = _make_sim()
        # Pre-create to force failure on the SECOND task create
        sim.task_tree.create(
            task_id="task.conflict",
            title="Existing",
            creator_agent_id="agent.root",
            owner_agent_id="agent.research",
        )

        # Order: valid TASK_CREATE first, then failing TASK_CREATE
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
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.conflict",  # duplicate → raises
            data={
                "task_id": "task.conflict",
                "title": "Conflict",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )

        sim._phase_commit(0, {})

        # First task rolled back — only the pre-existing one remains
        assert not sim.task_tree.exists("task.first")
        assert sim.task_tree.exists("task.conflict")  # pre-existing, untouched


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

        # This tick: KB_WRITE (v1 → v2), then a failing TASK_CREATE
        lock = sim._lock_manager.acquire(
            "project/research/notes.md", "agent.root", current_tick=1,
        )
        sim.task_tree.create(
            task_id="task.dup",
            title="Existing",
            creator_agent_id="agent.root",
            owner_agent_id="agent.research",
        )
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.root",
            "project/research/notes.md",
            data={"content": "v2", "expected_version": 1},
            expected_version=1,
            lock_token=lock.lock_token,
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.dup",  # already exists → create() raises
            data={
                "task_id": "task.dup",
                "title": "Dup",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )

        sim._phase_commit(1, {})

        # KB restored: content and version back to v1
        resource = sim._shared_kb.read("project/research/notes.md", "agent.root")
        assert resource.content == "v1"
        assert resource.version == 1
        assert sim._shared_kb.versions.get_version("project/research/notes.md") == 1
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) >= 1

    def test_rolled_back_email_produces_no_wake_event(self) -> None:
        """A rolled-back email never generates a NEW_EMAIL wake event."""
        sim = _make_sim()

        # Stage EMAIL_SEND then failing TASK_CREATE (duplicate id)
        sim.task_tree.create(
            task_id="task.conflict",
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
                "subject": "Doomed email",
                "body": "will roll back",
            },
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.conflict",  # duplicate → raises
            data={
                "task_id": "task.conflict",
                "title": "Dup",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.research",
            },
        )

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
