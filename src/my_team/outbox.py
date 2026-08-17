"""Reliable email outbox with idempotent delivery.

Per the reliability review: emails must not be directly queued into
MailSystem during commit. Instead they go through an outbox:

  STAGED → COMMITTED → DISPATCHING → DISPATCHED / FAILED

Each entry carries:
  - effect_id: originating staged effect
  - idempotency_key: prevents duplicate delivery (stable per email)
  - status: lifecycle state
  - attempt_count, last_error, next_retry_tick

The dispatch pipeline (called by the simulation after commit) processes
COMMITTED entries. Failed dispatches are retried up to max_retries.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class OutboxStatus(str, Enum):
    """Lifecycle status of an outbox entry."""

    STAGED = "staged"            # Created during Act phase
    COMMITTED = "committed"      # Commit succeeded, waiting for dispatch
    DISPATCHING = "dispatching"  # Dispatch in progress
    DISPATCHED = "dispatched"    # Successfully handed to MailSystem
    FAILED = "failed"            # Dispatch failed (retryable)
    DEAD = "dead"                # Exceeded max retries


class OutboxEntry(BaseModel):
    """A single email awaiting dispatch to the MailSystem."""

    entry_id: str = Field(
        default_factory=lambda: f"out.{uuid.uuid4().hex[:12]}",
        description="Unique outbox entry identifier",
    )
    idempotency_key: str = Field(
        description="Stable key — duplicate entries with the same key "
                    "are rejected",
    )
    effect_id: str = Field(default="", description="Originating staged effect")
    status: OutboxStatus = Field(default=OutboxStatus.STAGED)

    # Email payload
    from_agent: str = Field(description="Sender")
    to: list[str] = Field(description="Recipients")
    subject: str = Field(description="Subject")
    body: str = Field(default="", description="Body")
    email_type: str = Field(default="progress", description="Email type")
    task_id: str = Field(default="", description="Associated task")

    # Dispatch tracking
    attempt_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    last_error: str = Field(default="")
    next_retry_tick: int | None = Field(default=None)
    dispatched_at_tick: int | None = Field(default=None)


class Outbox:
    """Email outbox with idempotency and retry."""

    def __init__(self, max_retries: int = 3) -> None:
        self._entries: dict[str, OutboxEntry] = {}
        self._idempotency_keys: set[str] = set()
        self._max_retries = max_retries

    def stage(
        self,
        from_agent: str,
        to: list[str],
        subject: str,
        body: str = "",
        email_type: str = "progress",
        task_id: str = "",
        effect_id: str = "",
        idempotency_key: str = "",
    ) -> OutboxEntry:
        """Stage an email for dispatch (idempotency-checked).

        If an entry with the same idempotency_key already exists, the
        existing entry is returned (no duplicate).
        """
        key = idempotency_key or self._make_key(from_agent, subject, task_id)
        if key in self._idempotency_keys:
            for entry in self._entries.values():
                if entry.idempotency_key == key:
                    return entry
        entry = OutboxEntry(
            idempotency_key=key,
            effect_id=effect_id,
            from_agent=from_agent,
            to=to,
            subject=subject,
            body=body,
            email_type=email_type,
            task_id=task_id,
            max_retries=self._max_retries,
        )
        self._entries[entry.entry_id] = entry
        self._idempotency_keys.add(key)
        return entry

    def commit(self, entry_id: str) -> OutboxEntry | None:
        """Mark an entry as committed (after transaction commit)."""
        entry = self._entries.get(entry_id)
        if entry and entry.status == OutboxStatus.STAGED:
            entry.status = OutboxStatus.COMMITTED
        return entry

    def dispatch(
        self,
        deliver: Any,
        current_tick: int,
    ) -> tuple[list[OutboxEntry], list[OutboxEntry]]:
        """Dispatch all COMMITTED entries via the deliver callback.

        Returns (dispatched, failed). Failed entries are retried
        (next_retry_tick = current_tick + 1) up to max_retries, then
        marked DEAD.

        deliver is a callable(entry) that performs the actual email
        creation; it raises on failure.
        """
        dispatched: list[OutboxEntry] = []
        failed: list[OutboxEntry] = []

        for entry in list(self._entries.values()):
            if entry.status != OutboxStatus.COMMITTED:
                continue
            if (
                entry.next_retry_tick is not None
                and current_tick < entry.next_retry_tick
            ):
                continue

            entry.status = OutboxStatus.DISPATCHING
            try:
                deliver(entry)
                entry.status = OutboxStatus.DISPATCHED
                entry.dispatched_at_tick = current_tick
                dispatched.append(entry)
            except Exception as e:  # noqa: BLE001 — retryable failure
                entry.attempt_count += 1
                entry.last_error = str(e)
                if entry.attempt_count > entry.max_retries:
                    entry.status = OutboxStatus.DEAD
                else:
                    entry.status = OutboxStatus.COMMITTED
                    entry.next_retry_tick = current_tick + 1
                failed.append(entry)

        return dispatched, failed

    def rollback(self, entry_id: str) -> None:
        """Remove a staged (not yet committed) entry."""
        entry = self._entries.get(entry_id)
        if entry and entry.status == OutboxStatus.STAGED:
            self._idempotency_keys.discard(entry.idempotency_key)
            del self._entries[entry_id]

    def get(self, entry_id: str) -> OutboxEntry | None:
        return self._entries.get(entry_id)

    def entries_by_status(self, status: OutboxStatus) -> list[OutboxEntry]:
        return [e for e in self._entries.values() if e.status == status]

    @property
    def pending_count(self) -> int:
        """Entries not yet dispatched (staged/committed)."""
        return sum(
            1 for e in self._entries.values()
            if e.status in {OutboxStatus.STAGED, OutboxStatus.COMMITTED}
        )

    @property
    def dead_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.status == OutboxStatus.DEAD)

    @staticmethod
    def _make_key(from_agent: str, subject: str, task_id: str) -> str:
        return f"{from_agent}:{subject}:{task_id}:{uuid.uuid4().hex[:8]}"

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for e in self._entries.values():
            by_status[e.status.value] = by_status.get(e.status.value, 0) + 1
        return {"total": len(self._entries), "by_status": by_status}
