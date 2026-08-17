"""Tests for Phase 2: Email collaboration components.

Covers: mailbox, task tree, delegation protocol, result return.
"""

import pytest

from my_team.agent_tree import AgentTree
from my_team.delegation import (
    DelegationDeadlineError,
    DelegationDepthError,
    DelegationProtocol,
    NotDirectChildError,
)
from my_team.mailbox import MailSystem, Mailbox
from my_team.models.email import Email, EmailPriority, EmailStatus, EmailType
from my_team.models.task import Task, TaskPriority, TaskStatus
from my_team.task_tree import InvalidTransitionError, TaskTree, TaskNotFoundError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_agent_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {"agent_id": "agent.root", "display_name": "Root", "role": "root",
             "parent_id": None, "children": ["agent.research", "agent.planning"],
             "tools": ["read", "write", "ls", "delegate"], "can_delegate": True},
            {"agent_id": "agent.research", "display_name": "Research", "role": "research",
             "parent_id": "agent.root", "children": ["agent.web_research"],
             "tools": ["read", "write", "ls", "delegate"], "can_delegate": True},
            {"agent_id": "agent.planning", "display_name": "Planning", "role": "planning",
             "parent_id": "agent.root", "children": [],
             "tools": ["read", "write", "ls"], "can_delegate": False},
            {"agent_id": "agent.web_research", "display_name": "Web Research", "role": "web",
             "parent_id": "agent.research", "children": [],
             "tools": ["read", "write", "ls"], "can_delegate": False},
        ],
    })


@pytest.fixture
def mail_system() -> MailSystem:
    ms = MailSystem()
    ms.register_agent("agent.root")
    ms.register_agent("agent.research")
    ms.register_agent("agent.planning")
    ms.register_agent("agent.web_research")
    return ms


@pytest.fixture
def task_tree() -> TaskTree:
    return TaskTree()


@pytest.fixture
def protocol(sample_agent_tree, task_tree, mail_system) -> DelegationProtocol:
    return DelegationProtocol(sample_agent_tree, task_tree, mail_system)


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------

class TestMailbox:
    def test_create_mailbox(self):
        mb = Mailbox("agent.test")
        assert mb.agent_id == "agent.test"
        assert len(mb) == 0
        assert mb.unread_count == 0

    def test_receive_email(self):
        mb = Mailbox("agent.test")
        email = Email(
            email_id="mail.001",
            from_agent="agent.sender",
            to=["agent.test"],
            subject="Hello",
            email_type=EmailType.SYSTEM_NOTICE,
        )
        mb.receive(email)
        assert len(mb) == 1
        assert email.status == EmailStatus.DELIVERED

    def test_send_email(self):
        mb = Mailbox("agent.test")
        email = Email(
            email_id="mail.002",
            from_agent="agent.test",
            to=["agent.other"],
            subject="Report",
            email_type=EmailType.PROGRESS,
        )
        mb.send(email)
        assert len(mb.outbox) == 1
        assert email.status == EmailStatus.QUEUED

    def test_unread_count(self):
        mb = Mailbox("agent.test")
        for i in range(3):
            email = Email(
                email_id=f"mail.{i}",
                from_agent="agent.sender",
                to=["agent.test"],
                subject=f"Msg {i}",
                email_type=EmailType.SYSTEM_NOTICE,
            )
            mb.receive(email)
        assert mb.unread_count == 3

    def test_mark_read(self):
        mb = Mailbox("agent.test")
        email = Email(
            email_id="mail.001",
            from_agent="agent.sender",
            to=["agent.test"],
            subject="Read me",
            email_type=EmailType.SYSTEM_NOTICE,
        )
        mb.receive(email)
        assert mb.unread_count == 1
        mb.mark_read("mail.001")
        assert mb.unread_count == 0

    def test_mark_all_read(self):
        mb = Mailbox("agent.test")
        for i in range(5):
            email = Email(
                email_id=f"mail.{i}",
                from_agent="agent.sender",
                to=["agent.test"],
                subject=f"Msg {i}",
                email_type=EmailType.SYSTEM_NOTICE,
            )
            mb.receive(email)
        count = mb.mark_all_read()
        assert count == 5
        assert mb.unread_count == 0

    def test_get_by_type(self):
        mb = Mailbox("agent.test")
        types = [EmailType.DELEGATION, EmailType.PROGRESS, EmailType.DELEGATION]
        for i, t in enumerate(types):
            email = Email(
                email_id=f"mail.{i:03d}",
                from_agent="agent.sender",
                to=["agent.test"],
                subject="Test",
                email_type=t,
            )
            mb.receive(email)
        assert len(mb.get_by_type(EmailType.DELEGATION)) == 2
        assert len(mb.get_by_type(EmailType.PROGRESS)) == 1

    def test_get_by_task(self):
        mb = Mailbox("agent.test")
        email = Email(
            email_id="mail.001",
            from_agent="agent.sender",
            to=["agent.test"],
            subject="Task email",
            email_type=EmailType.DELEGATION,
            task_id="task.001",
        )
        mb.receive(email)
        assert len(mb.get_by_task("task.001")) == 1
        assert len(mb.get_by_task("task.999")) == 0

    def test_sorting_by_priority(self):
        mb = Mailbox("agent.test")
        for p in [EmailPriority.LOW, EmailPriority.URGENT, EmailPriority.NORMAL]:
            email = Email(
                email_id=f"mail.{p.value}",
                from_agent="agent.sender",
                to=["agent.test"],
                subject=f"Priority {p.value}",
                email_type=EmailType.SYSTEM_NOTICE,
                priority=p,
            )
            mb.receive(email)
        inbox = mb.inbox
        assert inbox[0].priority == EmailPriority.URGENT
        assert inbox[-1].priority == EmailPriority.LOW


# ---------------------------------------------------------------------------
# MailSystem
# ---------------------------------------------------------------------------

class TestMailSystem:
    def test_register_and_get(self):
        ms = MailSystem()
        mb = ms.register_agent("agent.a")
        assert mb.agent_id == "agent.a"
        assert ms.get_mailbox("agent.a") is mb

    def test_create_and_queue_email(self):
        ms = MailSystem()
        ms.register_agent("agent.a")
        ms.register_agent("agent.b")

        email = ms.create_email(
            from_agent="agent.a",
            to=["agent.b"],
            subject="Hello",
            body="World",
            email_type=EmailType.DELEGATION,
            tick=0,
        )
        assert email.email_id.startswith("mail.")
        assert ms.pending_count == 1

    def test_deliver_at_correct_tick(self):
        ms = MailSystem()
        ms.register_agent("agent.a")
        ms.register_agent("agent.b")

        email = ms.create_email(
            from_agent="agent.a",
            to=["agent.b"],
            subject="Test",
            email_type=EmailType.SYSTEM_NOTICE,
            tick=0,
            deliver_at_tick=2,
        )

        # Tick 0: not yet deliverable
        delivered = ms.deliver(0)
        assert len(delivered) == 0

        # Tick 1: still not deliverable
        delivered = ms.deliver(1)
        assert len(delivered) == 0

        # Tick 2: deliverable
        delivered = ms.deliver(2)
        assert len(delivered) == 1
        assert ms.get_mailbox("agent.b").unread_count == 1

    def test_deliver_to_multiple_recipients(self):
        ms = MailSystem()
        ms.register_agent("agent.a")
        ms.register_agent("agent.b")
        ms.register_agent("agent.c")

        ms.create_email(
            from_agent="agent.a",
            to=["agent.b", "agent.c"],
            subject="Broadcast",
            email_type=EmailType.SYSTEM_NOTICE,
            tick=0,
            deliver_at_tick=0,
        )

        ms.deliver(0)
        assert ms.get_mailbox("agent.b").unread_count == 1
        assert ms.get_mailbox("agent.c").unread_count == 1

    def test_sender_outbox(self):
        ms = MailSystem()
        ms.register_agent("agent.a")
        ms.register_agent("agent.b")

        ms.create_email(
            from_agent="agent.a",
            to=["agent.b"],
            subject="Test",
            email_type=EmailType.PROGRESS,
            tick=0,
        )

        outbox = ms.get_mailbox("agent.a").outbox
        assert len(outbox) == 1


# ---------------------------------------------------------------------------
# Task Tree
# ---------------------------------------------------------------------------

class TestTaskTree:
    def test_create_task(self):
        tree = TaskTree()
        task = tree.create(
            task_id="task.001",
            title="Research market",
            creator_agent_id="agent.root",
            owner_agent_id="agent.research",
            tick=0,
        )
        assert task.task_id == "task.001"
        assert task.status == TaskStatus.DRAFT
        assert tree.count() == 1

    def test_create_with_parent(self):
        tree = TaskTree()
        tree.create(
            task_id="task.001", title="Parent",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
        )
        child = tree.create(
            task_id="task.001.a", title="Child",
            creator_agent_id="agent.root", owner_agent_id="agent.research",
            parent_task_id="task.001",
        )
        assert child.parent_task_id == "task.001"
        assert len(tree.children("task.001")) == 1

    def test_duplicate_task_id_rejected(self):
        tree = TaskTree()
        tree.create(task_id="task.001", title="First",
                     creator_agent_id="a", owner_agent_id="a")
        with pytest.raises(Exception):
            tree.create(task_id="task.001", title="Second",
                         creator_agent_id="a", owner_agent_id="a")

    def test_parent_not_found(self):
        tree = TaskTree()
        with pytest.raises(TaskNotFoundError):
            tree.create(
                task_id="task.001", title="Orphan",
                creator_agent_id="a", owner_agent_id="a",
                parent_task_id="task.nonexistent",
            )

    def test_valid_status_transition(self):
        tree = TaskTree()
        task = tree.create(
            task_id="task.001", title="Test",
            creator_agent_id="a", owner_agent_id="a",
        )
        tree.update_status("task.001", TaskStatus.ASSIGNED, tick=1)
        assert task.status == TaskStatus.ASSIGNED

    def test_invalid_status_transition(self):
        tree = TaskTree()
        tree.create(
            task_id="task.001", title="Test",
            creator_agent_id="a", owner_agent_id="a",
        )
        with pytest.raises(InvalidTransitionError):
            tree.update_status("task.001", TaskStatus.COMPLETED, tick=0)

    def test_children_and_parent(self):
        tree = TaskTree()
        tree.create(task_id="task.001", title="Parent",
                     creator_agent_id="a", owner_agent_id="a")
        tree.create(task_id="task.001.a", title="Child A",
                     creator_agent_id="a", owner_agent_id="a",
                     parent_task_id="task.001")
        tree.create(task_id="task.001.b", title="Child B",
                     creator_agent_id="a", owner_agent_id="a",
                     parent_task_id="task.001")

        children = tree.children("task.001")
        assert len(children) == 2

        parent = tree.parent("task.001.a")
        assert parent.task_id == "task.001"

    def test_get_owner_tasks(self):
        tree = TaskTree()
        tree.create(task_id="t1", title="T1",
                     creator_agent_id="a", owner_agent_id="agent.research")
        tree.create(task_id="t2", title="T2",
                     creator_agent_id="a", owner_agent_id="agent.planning")
        tree.create(task_id="t3", title="T3",
                     creator_agent_id="a", owner_agent_id="agent.research")

        tasks = tree.get_owner_tasks("agent.research")
        assert len(tasks) == 2

    def test_get_expired_tasks(self):
        tree = TaskTree()
        tree.create(task_id="t1", title="T1",
                     creator_agent_id="a", owner_agent_id="a",
                     deadline_tick=5)
        tree.create(task_id="t2", title="T2",
                     creator_agent_id="a", owner_agent_id="a",
                     deadline_tick=10)

        # Advance past t1's deadline
        tree.update_status("t1", TaskStatus.ASSIGNED, tick=0)
        tree.update_status("t1", TaskStatus.ACCEPTED, tick=1)
        tree.update_status("t1", TaskStatus.IN_PROGRESS, tick=2)

        expired = tree.get_expired_tasks(current_tick=7)
        assert len(expired) == 1
        assert expired[0].task_id == "t1"

    def test_is_ancestor(self):
        tree = TaskTree()
        tree.create(task_id="t1", title="Root",
                     creator_agent_id="a", owner_agent_id="a")
        tree.create(task_id="t1.a", title="Child",
                     creator_agent_id="a", owner_agent_id="a",
                     parent_task_id="t1")
        tree.create(task_id="t1.a.b", title="Grandchild",
                     creator_agent_id="a", owner_agent_id="a",
                     parent_task_id="t1.a")

        assert tree.is_ancestor("t1", "t1.a.b")
        assert not tree.is_ancestor("t1.a", "t1")

    def test_subtree(self):
        tree = TaskTree()
        tree.create(task_id="t1", title="Root",
                     creator_agent_id="a", owner_agent_id="a")
        tree.create(task_id="t1.a", title="A",
                     creator_agent_id="a", owner_agent_id="a",
                     parent_task_id="t1")
        tree.create(task_id="t1.b", title="B",
                     creator_agent_id="a", owner_agent_id="a",
                     parent_task_id="t1")

        subtree = tree.subtree("t1")
        assert len(subtree) == 3


# ---------------------------------------------------------------------------
# Delegation Protocol
# ---------------------------------------------------------------------------

class TestDelegation:
    def test_delegate_to_direct_child(self, protocol, sample_agent_tree):
        task, email = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Research market",
            description="Analyze 3 markets",
            tick=0,
        )
        assert task.owner_agent_id == "agent.research"
        assert task.status == TaskStatus.ASSIGNED
        assert email.email_type == EmailType.DELEGATION
        assert "agent.research" in email.to

    def test_delegate_to_non_child_rejected(self, protocol):
        with pytest.raises(NotDirectChildError):
            protocol.delegate(
                delegator_id="agent.root",
                target_id="agent.web_research",  # not a direct child
                title="Direct delegation",
                tick=0,
            )

    def test_delegate_to_sibling_rejected(self, protocol):
        with pytest.raises(NotDirectChildError):
            protocol.delegate(
                delegator_id="agent.research",
                target_id="agent.planning",  # sibling, not child
                title="Cross delegation",
                tick=0,
            )

    def test_cascading_delegation(self, protocol):
        # Root → Research
        task1, _ = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Research task",
            tick=0,
        )

        # Research → Web Research
        task2, _ = protocol.delegate(
            delegator_id="agent.research",
            target_id="agent.web_research",
            title="Web research subtask",
            parent_task_id=task1.task_id,
            tick=1,
        )
        assert task2.parent_task_id == task1.task_id
        assert task2.owner_agent_id == "agent.web_research"

    def test_accept_task(self, protocol):
        task, _ = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Task",
            tick=0,
        )
        email = protocol.accept("agent.research", task.task_id, tick=1)
        assert email.email_type == EmailType.ACCEPTANCE
        assert task.status == TaskStatus.ACCEPTED

    def test_reject_task(self, protocol):
        task, _ = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Task",
            tick=0,
        )
        email = protocol.reject("agent.research", task.task_id,
                                reason="No resources", tick=1)
        assert email.email_type == EmailType.FAILURE
        assert task.status == TaskStatus.FAILED

    def test_submit_result(self, protocol):
        task, _ = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Task",
            tick=0,
        )
        protocol.accept("agent.research", task.task_id, tick=1)
        # Must transition through IN_PROGRESS before SUBMITTED
        task.transition_to(TaskStatus.IN_PROGRESS, tick=1)
        protocol.submit_result(
            agent_id="agent.research",
            task_id=task.task_id,
            summary="Analysis complete",
            artifacts=[{"path": "shared-kb/report.md", "version": 1}],
            tick=2,
        )
        assert task.status == TaskStatus.SUBMITTED

    def test_report_blocked(self, protocol):
        task, _ = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Task",
            tick=0,
        )
        protocol.accept("agent.research", task.task_id, tick=1)
        # Must transition through IN_PROGRESS before BLOCKED
        task.transition_to(TaskStatus.IN_PROGRESS, tick=1)
        email = protocol.report_blocked(
            "agent.research", task.task_id,
            reason="Missing data source", tick=2,
        )
        assert email.email_type == EmailType.BLOCKED
        assert task.status == TaskStatus.BLOCKED

    def test_cancel_task(self, protocol):
        task, _ = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Task",
            tick=0,
        )
        email = protocol.cancel("agent.root", task.task_id,
                                reason="No longer needed", tick=1)
        assert email.email_type == EmailType.CANCELLATION
        assert task.status == TaskStatus.CANCELLED

    def test_deadline_constraint(self, protocol):
        # Create parent with tight deadline
        parent_task, _ = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Parent task",
            deadline_tick=10,
            tick=0,
        )

        # Sub-task with later deadline should fail
        with pytest.raises(DelegationDeadlineError):
            protocol.delegate(
                delegator_id="agent.research",
                target_id="agent.web_research",
                title="Sub task",
                parent_task_id=parent_task.task_id,
                deadline_tick=15,  # exceeds parent's 10
                tick=1,
            )

    def test_full_delegation_lifecycle(self, protocol):
        """Test complete delegation flow: delegate → accept → work → submit."""
        # Root delegates to Research
        task, email = protocol.delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Market analysis",
            description="Analyze 3 markets",
            tick=0,
        )
        assert task.status == TaskStatus.ASSIGNED

        # Research accepts
        protocol.accept("agent.research", task.task_id, tick=1)
        assert task.status == TaskStatus.ACCEPTED

        # Research starts work
        task.status = TaskStatus.IN_PROGRESS

        # Research reports progress
        protocol.report_progress(
            "agent.research", task.task_id,
            message="Data collection 50% complete", tick=3,
        )

        # Research submits result
        protocol.submit_result(
            agent_id="agent.research",
            task_id=task.task_id,
            summary="Analysis complete. Found 2 viable markets.",
            artifacts=[{"path": "shared-kb/analysis.md", "version": 1}],
            limitations=["Limited data for market C"],
            recommendation="Focus on markets A and B",
            tick=5,
        )
        assert task.status == TaskStatus.SUBMITTED
