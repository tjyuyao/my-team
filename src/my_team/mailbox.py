"""Mailbox system for inter-agent email communication.

Per SPEC §4.5, §5.1, §13.3:
- Each agent has inbox/outbox
- Emails are delivered at specified ticks
- Email ordering follows priority rules
- Status: queued → delivered → read

N1c-1: MailSystem 归位为 Device 子类（SPEC §5.6，N1c 设备适配层）。
注册受控 uuid（范围级 DATA + 工具面 TOOL）+ InjectionDecl。
构造签名保持完全兼容（simulation.py 不变）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from my_team.devices.base import Device, EntityKind, InjectionDecl
from my_team.models.email import Email, EmailPriority, EmailStatus, EmailType

# Email ordering priority (lower = higher priority in sort)
_PRIORITY_ORDER = {
    EmailPriority.URGENT: 0,
    EmailPriority.HIGH: 1,
    EmailPriority.NORMAL: 2,
    EmailPriority.LOW: 3,
}

# Sentinel for "no deadline" — sorts after every real deadline.
_NO_DEADLINE = datetime.max.replace(tzinfo=timezone.utc)


def _sort_key(email: Email) -> tuple[int, int, datetime, int, str]:
    """Sort key per SPEC §13.3 ordering rules.

    Order:
    1. system_notice (rank 0)
    2. human_message (rank 1)
    3. priority (urgent=0, high=1, normal=2, low=3)
    4. deadline (real-calendar time; earlier first, None = last)
    5. created_at_tick (earlier first — lower tick = higher priority)
    6. email_id (deterministic tiebreak)
    """
    # Type rank: system_notice=0, human_message=1, others=2
    type_rank = 0
    if email.email_type == EmailType.HUMAN_MESSAGE:
        type_rank = 1
    elif email.email_type != EmailType.SYSTEM_NOTICE:
        type_rank = 2

    # Deadline rank: emails with earlier deadlines sort first.
    # Emails without a deadline (None) sort after those with deadlines.
    deadline_rank = email.deadline if email.deadline is not None else _NO_DEADLINE

    return (
        type_rank,
        _PRIORITY_ORDER.get(email.priority, 2),
        deadline_rank,
        email.created_at_tick,
        email.email_id,
    )


class Mailbox:
    """Manages email storage and delivery for a single agent.

    Provides inbox (received) and outbox (sent) management with
    proper status tracking and sorting.
    """

    def __init__(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._inbox: dict[str, Email] = {}  # email_id → Email
        self._outbox: dict[str, Email] = {}

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def inbox(self) -> list[Email]:
        """All emails in inbox, sorted by priority."""
        return sorted(self._inbox.values(), key=_sort_key)

    @property
    def outbox(self) -> list[Email]:
        """All emails in outbox."""
        return sorted(self._outbox.values(), key=_sort_key)

    @property
    def unread_count(self) -> int:
        """Number of unread emails in inbox."""
        return sum(
            1 for e in self._inbox.values()
            if e.status == EmailStatus.DELIVERED
        )

    def receive(self, email: Email) -> None:
        """Add an email to the inbox (called by system during Deliver phase)."""
        email.mark_delivered()
        self._inbox[email.email_id] = email

    def send(self, email: Email) -> None:
        """Add an email to the outbox (called when agent sends)."""
        email.status = EmailStatus.QUEUED
        self._outbox[email.email_id] = email

    def get_unread(self) -> list[Email]:
        """Get all unread emails from inbox."""
        return [
            e for e in self.inbox
            if e.status == EmailStatus.DELIVERED
        ]

    def get_by_type(self, email_type: EmailType) -> list[Email]:
        """Get all emails of a specific type from inbox."""
        return [e for e in self.inbox if e.email_type == email_type]

    def get_by_task(self, task_id: str) -> list[Email]:
        """Get all emails related to a specific task."""
        return [e for e in self.inbox if e.task_id == task_id]

    def get_thread(self, thread_id: str) -> list[Email]:
        """Get all emails in a thread, sorted by creation time."""
        return sorted(
            [e for e in self._inbox.values() if e.thread_id == thread_id],
            key=lambda e: e.created_at_tick,
        )

    def mark_read(self, email_id: str) -> bool:
        """Mark an email as read. Returns True if found and updated."""
        email = self._inbox.get(email_id)
        if email is None:
            return False
        email.mark_read()
        return True

    def mark_all_read(self) -> int:
        """Mark all inbox emails as read. Returns count marked."""
        count = 0
        for email in self._inbox.values():
            if email.status == EmailStatus.DELIVERED:
                email.mark_read()
                count += 1
        return count

    def get_email(self, email_id: str) -> Email | None:
        """Get a specific email by ID from inbox."""
        return self._inbox.get(email_id)

    def remove_from_inbox(self, email_id: str) -> bool:
        """Remove an email from inbox. Returns True if found."""
        return self._inbox.pop(email_id, None) is not None

    def clear_outbox(self) -> list[Email]:
        """Clear and return all emails from outbox."""
        emails = list(self._outbox.values())
        self._outbox.clear()
        return emails

    def __len__(self) -> int:
        return len(self._inbox)

    def __repr__(self) -> str:
        return (
            f"Mailbox({self._agent_id}, "
            f"inbox={len(self._inbox)}, outbox={len(self._outbox)})"
        )


class MailSystem(Device):
    """Central email system that manages all agent mailboxes.

    Handles email routing, delivery, and cross-agent communication.

    N1c-1 设备归位：继承 Device，构造时注册受控 uuid
    （范围级 DATA + 工具面 TOOL）并声明 InjectionDecl。
    构造签名保持原样（simulation.py 兼容）。
    """

    def __init__(self, device_id: str | None = None) -> None:
        Device.__init__(self, device_id)
        self._mailboxes: dict[str, Mailbox] = {}
        self._pending: list[Email] = []  # emails waiting to be delivered
        self._all_emails: dict[str, Email] = {}  # global email registry
        # N1c-1：注册设备受控实体
        # 范围级 DATA 实体 — 邮件系统整体范围，InjectionDecl 引导 bash
        self.mail_scope_id = self.register_entity(
            EntityKind.DATA,
            "mail-system-scope",
            injection=InjectionDecl(
                content=(
                    "[MAIL_INSTRUCTION] 邮件系统（MailSystem）是 bash 间通信的主要渠道。\n"
                    "通过 send_email 工具（STAGED_MUTATION）发送邮件，"
                    "邮件在下一 tick 送达收件人邮箱。\n"
                    "邮件按优先级和截止时间排序：system_notice > human_message > 其他；"
                    "大附件使用 AttachmentRef 引用而非内嵌内容。"
                ),
                source_tag="[MAIL_INSTRUCTION]",
            ),
        )
        # 工具面 TOOL 实体 — 采用 uuid5 派生值（adopt 机制）
        from my_team.tool_manifest import builtin_manifests
        _manifests = builtin_manifests()
        self.send_email_capability = self.register_entity(
            EntityKind.TOOL, "send_email",
            entity_id=_manifests["send_email"].capability,
        )

    def register_agent(self, agent_id: str) -> Mailbox:
        """Create and register a mailbox for an agent."""
        mailbox = Mailbox(agent_id)
        self._mailboxes[agent_id] = mailbox
        return mailbox

    def get_mailbox(self, agent_id: str) -> Mailbox | None:
        """Get an agent's mailbox."""
        return self._mailboxes.get(agent_id)

    def queue_email(self, email: Email) -> None:
        """Queue an email for delivery at its deliver_at_tick."""
        self._pending.append(email)
        self._all_emails[email.email_id] = email

    def deliver(self, current_tick: int) -> list[Email]:
        """Deliver all emails whose deliver_at_tick <= current_tick.

        Per SPEC §13.3 timing semantics:
        - Emails created at tick t are earliest deliverable at tick t+1
        - If deliver_at_tick < created_at_tick, it is auto-corrected
        - Emails created in tick t's Act phase are NOT visible in tick t's Observe

        Returns list of delivered emails.
        """
        delivered: list[Email] = []
        remaining: list[Email] = []

        for email in self._pending:
            # Auto-correct: deliver_at_tick must be > created_at_tick
            earliest_deliver = email.created_at_tick + 1
            if email.deliver_at_tick < earliest_deliver:
                email.deliver_at_tick = earliest_deliver

            if email.deliver_at_tick <= current_tick:
                # Route to all recipients
                for recipient_id in email.to:
                    mailbox = self._mailboxes.get(recipient_id)
                    if mailbox is not None:
                        mailbox.receive(email)
                delivered.append(email)
            else:
                remaining.append(email)

        self._pending = remaining
        return delivered

    def create_email(
        self,
        from_agent: str,
        to: list[str],
        subject: str,
        body: str = "",
        email_type: EmailType = EmailType.SYSTEM_NOTICE,
        task_id: str = "",
        tick: int = 0,
        deliver_at_tick: int | None = None,
        priority: EmailPriority = EmailPriority.NORMAL,
        requires_reply: bool = False,
        reply_to: str | None = None,
        thread_id: str = "",
        attachments: list[Any] | None = None,
        **kwargs: Any,
    ) -> Email:
        """Create and queue a new email.

        Returns the created email (not yet delivered).
        """
        email_id = f"mail.{len(self._all_emails) + 1:06d}"
        email = Email(
            email_id=email_id,
            thread_id=thread_id or f"thread.{task_id}" if task_id else "",
            from_agent=from_agent,
            to=to,
            subject=subject,
            body=body,
            attachments=list(attachments) if attachments else [],
            email_type=email_type,
            task_id=task_id,
            created_at_tick=tick,
            deliver_at_tick=deliver_at_tick if deliver_at_tick is not None else tick + 1,
            priority=priority,
            requires_reply=requires_reply,
            reply_to=reply_to,
            **kwargs,
        )

        # Add to sender's outbox
        sender_mailbox = self._mailboxes.get(from_agent)
        if sender_mailbox is not None:
            sender_mailbox.send(email)

        self.queue_email(email)
        return email

    def get_email(self, email_id: str) -> Email | None:
        """Get any email by ID from the global registry."""
        return self._all_emails.get(email_id)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def __repr__(self) -> str:
        return (
            f"MailSystem(mailboxes={len(self._mailboxes)}, "
            f"pending={len(self._pending)})"
        )
