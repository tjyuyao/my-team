"""Email data model for inter-agent communication.

Per SPEC §4.5:
- Email is the sole formal collaboration channel between agents
- 13 email types supported
- Emails carry references, not large content
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from my_team.asset_store import AttachmentRef


class EmailType(str, Enum):
    """Supported email types per SPEC §4.5."""

    DELEGATION = "delegation"
    ACCEPTANCE = "acceptance"
    PROGRESS = "progress"
    QUESTION = "question"
    ANSWER = "answer"
    RESULT = "result"
    REVIEW_REQUEST = "review_request"
    REVIEW_RESULT = "review_result"
    FAILURE = "failure"
    BLOCKED = "blocked"
    CANCELLATION = "cancellation"
    HUMAN_MESSAGE = "human_message"
    SYSTEM_NOTICE = "system_notice"


class EmailStatus(str, Enum):
    """Email lifecycle status."""

    QUEUED = "queued"
    DELIVERED = "delivered"
    READ = "read"


class EmailPriority(str, Enum):
    """Email priority levels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class Email(BaseModel):
    """An email message between agents.

    Large content should be stored in private workspace or shared KB;
    emails carry only references, paths, and version numbers.
    """

    email_id: str = Field(description="Unique identifier, e.g. 'mail.000123'")
    thread_id: str = Field(
        default="",
        description="Email thread identifier, e.g. 'thread.task.2026.001'",
    )
    from_agent: str = Field(description="Sender agent ID")
    to: list[str] = Field(description="Recipient agent IDs")
    cc: list[str] = Field(default_factory=list, description="CC agent IDs")
    subject: str = Field(description="Email subject line")
    body: str = Field(default="", description="Email body text")
    # v0.10 T8b: structured attachment references (SPEC §4.3) — 大内容
    # 只存引用，不复制正文。ref_type: 'shared_kb' | 'asset' (T10).
    attachments: list[AttachmentRef] = Field(
        default_factory=list,
        description="Structured attachment references",
    )
    email_type: EmailType = Field(description="Type of email")
    task_id: str = Field(default="", description="Associated task ID")
    created_at_tick: int = Field(default=0, description="Tick when email was created")
    deliver_at_tick: int = Field(default=0, description="Tick when email should be delivered")
    status: EmailStatus = Field(default=EmailStatus.QUEUED)
    priority: EmailPriority = Field(default=EmailPriority.NORMAL)
    requires_reply: bool = Field(default=False, description="Whether a reply is expected")
    reply_to: str | None = Field(default=None, description="Email ID this is replying to")
    deadline: datetime | None = Field(
        default=None,
        description=(
            "Associated task deadline for sort priority (SPEC §13.3; "
            "real-calendar time per §9.1)"
        ),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    def mark_delivered(self) -> None:
        self.status = EmailStatus.DELIVERED

    def mark_read(self) -> None:
        self.status = EmailStatus.READ
