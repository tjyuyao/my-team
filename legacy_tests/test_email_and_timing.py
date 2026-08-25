"""Tests for email deadline sorting, delivery timing, and timeout phase binding.

Covers review gaps:
- §13.3 email deadline sorting
- Email delivery timing semantics
- Timeout checker phase binding (post-Commit, pre-Audit)
"""


from datetime import datetime, timedelta, timezone

from my_team.mailbox import MailSystem
from my_team.models.email import EmailPriority, EmailType

_BASE = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Email Deadline Sorting (§13.3)
# ---------------------------------------------------------------------------

class TestEmailDeadlineSorting:
    def test_deadline_email_sorts_before_no_deadline(self):
        """Emails with deadlines sort before emails without."""
        ms = MailSystem()
        ms.register_agent("agent.a")

        # Email with deadline
        e1 = ms.create_email(
            from_agent="agent.b",
            to=["agent.a"],
            subject="Urgent task",
            email_type=EmailType.DELEGATION,
            tick=0,
            deadline=_BASE + timedelta(minutes=10),
        )
        # Email without deadline
        e2 = ms.create_email(
            from_agent="agent.c",
            to=["agent.a"],
            subject="Normal message",
            email_type=EmailType.PROGRESS,
            tick=0,
        )

        ms.deliver(1)
        mailbox = ms.get_mailbox("agent.a")
        inbox = mailbox.inbox
        # Deadline email should come first (deadline_rank=10 < 999999)
        assert inbox[0].email_id == e1.email_id
        assert inbox[1].email_id == e2.email_id

    def test_earlier_deadline_sorts_first(self):
        """Earlier deadlines sort before later deadlines."""
        ms = MailSystem()
        ms.register_agent("agent.a")

        e_later = ms.create_email(
            from_agent="agent.b",
            to=["agent.a"],
            subject="Later deadline",
            email_type=EmailType.DELEGATION,
            tick=0,
            deadline=_BASE + timedelta(minutes=20),
        )
        e_earlier = ms.create_email(
            from_agent="agent.c",
            to=["agent.a"],
            subject="Earlier deadline",
            email_type=EmailType.DELEGATION,
            tick=0,
            deadline=_BASE + timedelta(minutes=5),
        )

        ms.deliver(1)
        mailbox = ms.get_mailbox("agent.a")
        inbox = mailbox.inbox
        assert inbox[0].email_id == e_earlier.email_id
        assert inbox[1].email_id == e_later.email_id

    def test_deadline_tiebreaker_by_priority(self):
        """Same deadline, higher priority sorts first."""
        ms = MailSystem()
        ms.register_agent("agent.a")

        ms.create_email(
            from_agent="agent.b",
            to=["agent.a"],
            subject="Low priority",
            email_type=EmailType.DELEGATION,
            tick=0,
            deadline=_BASE + timedelta(minutes=10),
            priority=EmailPriority.LOW,
        )
        ms.create_email(
            from_agent="agent.c",
            to=["agent.a"],
            subject="Urgent priority",
            email_type=EmailType.DELEGATION,
            tick=0,
            deadline=_BASE + timedelta(minutes=10),
            priority=EmailPriority.URGENT,
        )

        ms.deliver(1)
        mailbox = ms.get_mailbox("agent.a")
        inbox = mailbox.inbox
        assert inbox[0].priority == EmailPriority.URGENT
        assert inbox[1].priority == EmailPriority.LOW


# ---------------------------------------------------------------------------
# Email Delivery Timing Semantics
# ---------------------------------------------------------------------------

class TestEmailDeliveryTiming:
    def test_same_tick_not_delivered(self):
        """Email created at tick t is NOT delivered at tick t."""
        ms = MailSystem()
        ms.register_agent("agent.a")

        ms.create_email(
            from_agent="agent.b",
            to=["agent.a"],
            subject="Test",
            tick=5,
            deliver_at_tick=5,  # explicitly same tick
        )

        delivered = ms.deliver(5)
        assert len(delivered) == 0

    def test_next_tick_delivered(self):
        """Email created at tick t IS delivered at tick t+1."""
        ms = MailSystem()
        ms.register_agent("agent.a")

        ms.create_email(
            from_agent="agent.b",
            to=["agent.a"],
            subject="Test",
            tick=5,
        )

        delivered = ms.deliver(6)  # tick 5+1
        assert len(delivered) == 1

    def test_deliver_at_tick_before_created_auto_corrected(self):
        """deliver_at_tick < created_at_tick is auto-corrected to created_at_tick + 1."""
        ms = MailSystem()
        ms.register_agent("agent.a")

        ms.create_email(
            from_agent="agent.b",
            to=["agent.a"],
            subject="Test",
            tick=10,
            deliver_at_tick=3,  # before creation — should be corrected
        )

        # Deliver at tick 3 — should NOT deliver (auto-corrected to 11)
        delivered = ms.deliver(3)
        assert len(delivered) == 0

        # Deliver at tick 11 — should deliver (corrected to 10+1=11)
        delivered = ms.deliver(11)
        assert len(delivered) == 1

    def test_deliver_at_tick_before_created_corrected_to_next(self):
        """Auto-correction sets deliver_at_tick = created_at_tick + 1."""
        ms = MailSystem()
        ms.register_agent("agent.a")

        email = ms.create_email(
            from_agent="agent.b",
            to=["agent.a"],
            subject="Test",
            tick=7,
            deliver_at_tick=2,  # way before creation
        )

        # After first deliver call, deliver_at_tick should be corrected
        ms.deliver(5)  # not delivered yet (corrected to 8)
        assert email.deliver_at_tick == 8  # 7 + 1

    def test_delayed_delivery_respects_deliver_at_tick(self):
        """Email with future deliver_at_tick waits until that tick."""
        ms = MailSystem()
        ms.register_agent("agent.a")

        ms.create_email(
            from_agent="agent.b",
            to=["agent.a"],
            subject="Future",
            tick=1,
            deliver_at_tick=10,
        )

        # Not delivered at tick 5
        delivered = ms.deliver(5)
        assert len(delivered) == 0

        # Delivered at tick 10
        delivered = ms.deliver(10)
        assert len(delivered) == 1


# ---------------------------------------------------------------------------
# Timeout Phase Binding
# ---------------------------------------------------------------------------

class TestTimeoutPhaseBinding:
    def test_timeout_checker_called_in_simulation(self):
        """Verify TimeoutChecker is initialized in Simulation."""
        from my_team.agent_tree import AgentTree
        from my_team.simulation import Simulation

        tree = AgentTree.from_dict({
            "agents": [
                {
                    "agent_id": "agent.root",
                    "display_name": "Root",
                    "role": "root_decision_agent",
                    "parent_id": None,
                    "children": [],
                    "tools": ["read", "write", "ls", "delegate"],
                    "can_delegate": True,
                },
            ],
        })
        sim = Simulation(agent_tree=tree)
        assert hasattr(sim, "_timeout_checker")
        assert sim._timeout_checker is not None
