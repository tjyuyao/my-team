"""Comprehensive audit logging system.

Per SPEC §8.2 Phase 7, §15.3:
- All state changes must be auditable
- Log covers: agent lifecycle, delegation, email, tool calls, file ops,
  shared KB changes, lock events, permission denials, human ops, tick advances
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AuditEventType(str, Enum):
    """Categories of auditable events."""

    # Agent lifecycle
    AGENT_CREATED = "agent.created"
    AGENT_INITIALIZED = "agent.initialized"
    AGENT_TERMINATED = "agent.terminated"
    AGENT_STATE_CHANGED = "agent.state_changed"

    # Delegation
    DELEGATION_SENT = "delegation.sent"
    DELEGATION_ACCEPTED = "delegation.accepted"
    DELEGATION_REJECTED = "delegation.rejected"

    # Email
    EMAIL_SENT = "email.sent"
    EMAIL_RECEIVED = "email.received"
    EMAIL_DELIVERED = "email.delivered"
    EMAIL_DELIVERY_FAILED = "email.delivery_failed"

    # Tool calls
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_DISPATCHED = "tool.dispatched"  # Executor admission/dispatch (v0.8)

    # File operations
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    FILE_DELETE = "file.delete"

    # Shared KB
    SHARED_KB_CREATE = "shared_kb.create"
    SHARED_KB_READ = "shared_kb.read"
    SHARED_KB_WRITE = "shared_kb.write"
    SHARED_KB_DELETE = "shared_kb.delete"

    # Locks
    LOCK_ACQUIRED = "lock.acquired"
    LOCK_RELEASED = "lock.released"
    LOCK_RENEWED = "lock.renewed"
    LOCK_EXPIRED = "lock.expired"
    LOCK_CONFLICT = "lock.conflict"

    # Permissions
    PERMISSION_DENIED = "permission.denied"

    # Human operations
    HUMAN_PAUSE = "human.pause"
    HUMAN_RESUME = "human.resume"
    HUMAN_EMAIL = "human.email"
    HUMAN_CONFIG_CHANGE = "human.config_change"

    # Tick
    TICK_ADVANCE = "tick.advance"
    TICK_COMPLETE = "tick.complete"

    # Agent failure
    AGENT_FAILED = "agent.failed"
    AGENT_RETRY = "agent.retry"

    # Agent activation (event-driven scheduling)
    AGENT_ACTIVATED = "agent.activated"
    AGENT_WOKEN = "agent.woken"
    AGENT_IDLE = "agent.idle"

    # General
    CUSTOM = "custom"

    # Transaction
    TRANSACTION_COMMIT = "transaction.commit"
    TRANSACTION_ROLLBACK = "transaction.rollback"

    # Result fencing (v0.6.0 hardening)
    STALE_RESULT = "result.stale"

    # Operation lifecycle (v0.7.0 P1-4)
    OP_CANCELLED = "op.cancelled"
    TOOL_TIMEOUT = "tool.timeout"


class AuditEntry(BaseModel):
    """A single audit log entry."""

    event_id: int = Field(description="Sequential event ID")
    timestamp: str = Field(description="ISO-8601 UTC timestamp")
    event_type: AuditEventType = Field(description="Category of event")
    agent_id: str = Field(default="", description="Related agent")
    tick: int = Field(default=0, description="Simulation tick")
    details: dict[str, Any] = Field(default_factory=dict, description="Event details")
    success: bool = Field(default=True, description="Whether event succeeded")
    error: str | None = Field(default=None, description="Error if failed")


class AuditLog:
    """Append-only audit log for all system events.

    Provides write-once recording with query capabilities.
    All state changes should be recordable and reconstructable.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._counter = 0

    def record(
        self,
        event_type: AuditEventType,
        agent_id: str = "",
        tick: int = 0,
        details: dict[str, Any] | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> AuditEntry:
        """Record an audit event.

        Returns the created entry.
        """
        self._counter += 1
        entry = AuditEntry(
            event_id=self._counter,
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            agent_id=agent_id,
            tick=tick,
            details=details or {},
            success=success,
            error=error,
        )
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> list[AuditEntry]:
        """All audit entries (read-only copy)."""
        return list(self._entries)

    @property
    def count(self) -> int:
        return len(self._entries)

    def for_agent(self, agent_id: str) -> list[AuditEntry]:
        """Get all entries for a specific agent."""
        return [e for e in self._entries if e.agent_id == agent_id]

    def for_event_type(self, event_type: AuditEventType) -> list[AuditEntry]:
        """Get all entries of a specific event type."""
        return [e for e in self._entries if e.event_type == event_type]

    def for_tick(self, tick: int) -> list[AuditEntry]:
        """Get all entries for a specific tick."""
        return [e for e in self._entries if e.tick == tick]

    def failures(self) -> list[AuditEntry]:
        """Get all failed events."""
        return [e for e in self._entries if not e.success]

    def since(self, event_id: int) -> list[AuditEntry]:
        """Get entries after a specific event ID."""
        return [e for e in self._entries if e.event_id > event_id]

    def last(self, n: int = 1) -> list[AuditEntry]:
        """Get the last N entries."""
        return self._entries[-n:]

    def clear(self) -> None:
        """Clear all entries (for testing only)."""
        self._entries.clear()
        self._counter = 0

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"AuditLog({len(self._entries)} entries)"
