"""Tests for transaction rollback, outbox, and side_effect classification.

Covers review gap §8.4: transaction boundary clarification.
"""


from my_team.transaction import (
    _EXTERNAL_EFFECT_TYPES,
    EffectStatus,
    EffectType,
    TransactionBuffer,
)


class TestSideEffectClassification:
    """Verify that effect types are correctly classified."""

    def test_email_send_is_side_effect(self):
        buf = TransactionBuffer()
        effect = buf.stage(EffectType.EMAIL_SEND, "agent.a", "email:test")
        assert effect.side_effect is True

    def test_file_write_is_side_effect(self):
        buf = TransactionBuffer()
        effect = buf.stage(EffectType.FILE_WRITE, "agent.a", "file.txt")
        assert effect.side_effect is True

    def test_kb_write_not_side_effect(self):
        buf = TransactionBuffer()
        effect = buf.stage(EffectType.KB_WRITE, "agent.a", "project/report.md")
        assert effect.side_effect is False

    def test_task_create_not_side_effect(self):
        buf = TransactionBuffer()
        effect = buf.stage(EffectType.TASK_CREATE, "agent.a", "task:123")
        assert effect.side_effect is False

    def test_lock_acquire_not_side_effect(self):
        buf = TransactionBuffer()
        effect = buf.stage(EffectType.LOCK_ACQUIRE, "agent.a", "resource/a")
        assert effect.side_effect is False

    def test_all_external_types_classified(self):
        """All types in _EXTERNAL_EFFECT_TYPES should be side effects."""
        for et in _EXTERNAL_EFFECT_TYPES:
            buf = TransactionBuffer()
            effect = buf.stage(et, "agent.a", "test")
            assert effect.side_effect is True, f"{et} should be side_effect"


class TestOutbox:
    """Test outbox lifecycle during commit."""

    def test_outbox_empty_initially(self):
        buf = TransactionBuffer()
        assert buf.outbox_count == 0
        assert buf.get_outbox() == []

    def test_commit_populates_outbox(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.EMAIL_SEND, "agent.a", "email:hello")
        buf.stage(EffectType.KB_WRITE, "agent.a", "project/report.md")
        buf.validate()
        committed = buf.commit()
        assert len(committed) == 2
        assert buf.outbox_count == 1  # only EMAIL_SEND is a side effect

    def test_clear_outbox_returns_items(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.EMAIL_SEND, "agent.a", "email:hello")
        buf.validate()
        buf.commit()
        items = buf.clear_outbox()
        assert len(items) == 1
        assert buf.outbox_count == 0

    def test_get_outbox_does_not_clear(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.EMAIL_SEND, "agent.a", "email:hello")
        buf.validate()
        buf.commit()
        items = buf.get_outbox()
        assert len(items) == 1
        assert buf.outbox_count == 1  # still there


class TestRollback:
    """Test rollback clears outbox and marks effects."""

    def test_rollback_marks_effects(self):
        buf = TransactionBuffer()
        e1 = buf.stage(EffectType.KB_WRITE, "agent.a", "project/a.md")
        e2 = buf.stage(EffectType.TASK_CREATE, "agent.a", "task:1")
        buf.validate()
        buf.commit()
        rolled = buf.rollback()
        assert len(rolled) == 2
        assert e1.status == EffectStatus.ROLLED_BACK
        assert e2.status == EffectStatus.ROLLED_BACK

    def test_rollback_clears_outbox(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.EMAIL_SEND, "agent.a", "email:hello")
        buf.stage(EffectType.KB_WRITE, "agent.a", "project/a.md")
        buf.validate()
        buf.commit()
        assert buf.outbox_count == 1
        buf.rollback()
        assert buf.outbox_count == 0

    def test_rollback_returns_only_committed(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "project/a.md")
        # Don't validate — effect stays STAGED
        rolled = buf.rollback()
        assert len(rolled) == 0  # nothing was committed

    def test_clear_resets_everything(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.EMAIL_SEND, "agent.a", "email:hello")
        buf.validate()
        buf.commit()
        buf.clear()
        assert buf.outbox_count == 0
        assert len(buf.get_effects()) == 0
        assert not buf.has_pending


class TestTransactionalEmailFlow:
    """End-to-end: stage email → validate → commit → outbox → deliver."""

    def test_email_only_delivered_after_commit(self):
        """Emails should not be 'delivered' until outbox is processed."""
        buf = TransactionBuffer()
        buf.stage(
            EffectType.EMAIL_SEND, "agent.a", "email:report",
            data={"to": "agent.b", "subject": "Report"},
        )
        kb_effect = buf.stage(
            EffectType.KB_WRITE, "agent.a", "project/report.md",
            data={"content": "report data"},
        )

        # Validate and commit
        buf.validate()
        committed = buf.commit()
        assert len(committed) == 2

        # Outbox has only the email
        outbox = buf.get_outbox()
        assert len(outbox) == 1
        assert outbox[0].effect_type == EffectType.EMAIL_SEND

        # Simulate delivery after commit
        delivered = buf.clear_outbox()
        assert len(delivered) == 1

        # KB effect is committed (in-memory), not in outbox
        assert kb_effect.status == EffectStatus.COMMITTED

    def test_failed_commit_no_outbox_delivery(self):
        """If commit has no validated effects, outbox stays empty."""
        buf = TransactionBuffer()
        buf.stage(EffectType.EMAIL_SEND, "agent.a", "email:hello")
        # Don't validate — nothing gets committed
        committed = buf.commit()
        assert len(committed) == 0
        assert buf.outbox_count == 0
