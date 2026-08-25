"""Unified TickJournal — single source of truth for all state changes.

Per SPEC §3.2:
- Each tick produces one TickRecord (append-only)
- Contains: intents, validation, effects, pending ops, outbox, audit events
- Commit → record status=committed; rollback → status=aborted
- AuditLog is a projection over Journal entries
- PendingOps/Outbox registries remain authoritative; Journal records
  their per-tick changes for future replay capability

This is the in-memory implementation. SQLite persistence is deferred
to a later phase.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from my_team.audit import AuditEntry


class TickRecordStatus(str, Enum):
    """Status of a tick record in the journal."""

    COMMITTED = "committed"
    ABORTED = "aborted"


class IntentSummary(BaseModel):
    """Lightweight summary of a single intent from _phase_decide."""

    intent_id: str = Field(description="Intent identifier")
    intent_type: str = Field(description="Intent type value (e.g. submit_llm_request)")
    agent_id: str = Field(description="Agent that produced this intent")
    task_id: str = Field(default="", description="Related task ID")
    success: bool = Field(default=True, description="Did validation pass?")
    error: str | None = Field(default=None, description="Validation failure reason")


class EffectSummary(BaseModel):
    """Summary of a committed/failed effect from _phase_commit."""

    effect_id: str = Field(description="Effect identifier")
    effect_type: str = Field(description="EffectType value")
    agent_id: str = Field(description="Agent that produced this effect")
    resource: str = Field(description="Resource path or identifier")
    status: str = Field(description="EffectStatus value")
    error: str | None = Field(default=None, description="Error if failed")


class PendingOpSummary(BaseModel):
    """Summary of a pending op registered during this tick."""

    request_id: str = Field(description="Operation request ID")
    op_type: str = Field(description="OpType value")
    agent_id: str = Field(description="Agent that registered this op")
    created_tick: int = Field(description="Tick when op was created")


class OutboxSummary(BaseModel):
    """Summary of an outbox entry staged during this tick."""

    entry_id: str = Field(description="Outbox entry ID")
    effect_id: str = Field(description="Source effect ID")
    from_agent: str = Field(description="Sender agent")
    to: list[str] = Field(description="Recipient agents")
    subject: str = Field(description="Email subject")


class TickRecord(BaseModel):
    """A single tick's complete record in the journal.

    Captures everything that happened during one tick so that audit,
    replay, and recovery can reconstruct from Journal alone.
    """

    tick: int = Field(description="Tick number")
    epoch: int = Field(description="State epoch at tick start")
    snapshot_hash: str = Field(
        default="",
        description="Hash of the freeze snapshot at tick start",
    )
    intents: list[IntentSummary] = Field(
        default_factory=list,
        description="Intents produced by _phase_decide",
    )
    validation: list[IntentSummary] = Field(
        default_factory=list,
        description="Validation results from _phase_validate",
    )
    effects: list[EffectSummary] = Field(
        default_factory=list,
        description="Effects after _phase_commit (with final status)",
    )
    pending_ops: list[PendingOpSummary] = Field(
        default_factory=list,
        description="Pending ops registered during this tick",
    )
    outbox: list[OutboxSummary] = Field(
        default_factory=list,
        description="Outbox entries staged during this tick",
    )
    approvals: list[Any] = Field(
        default_factory=list,
        description="Approval requests (reserved, empty for now)",
    )
    audit_events: list[AuditEntry] = Field(
        default_factory=list,
        description="Audit events recorded during this tick",
    )
    status: TickRecordStatus = Field(
        default=TickRecordStatus.COMMITTED,
        description="Final status: committed or aborted",
    )
    error: str | None = Field(
        default=None,
        description="Error if tick was aborted",
    )


class TickJournal:
    """Append-only journal of per-tick records.

    Usage in run_tick():
      1. journal.start_tick(tick, epoch)   — before Phase 1
      2. ... phases execute, AuditLog.record() writes to current record ...
      3. ... _phase_commit writes effects/pending_ops/outbox ...
      4. journal.finalize(status)          — after Phase 10

    AuditLog holds a reference to this Journal and transparently writes
    audit events into the current TickRecord via current_record.
    """

    def __init__(self) -> None:
        self._records: list[TickRecord] = []
        self._current_record: TickRecord | None = None

    def start_tick(self, tick: int, epoch: int) -> TickRecord:
        """Create a new TickRecord for the current tick and set it as active.

        Called at the beginning of run_tick(), before Phase 1 Ingest.
        """
        record = TickRecord(tick=tick, epoch=epoch)
        self._records.append(record)
        self._current_record = record
        return record

    @property
    def current_record(self) -> TickRecord | None:
        """The active TickRecord for the current tick (None outside run_tick)."""
        return self._current_record

    def finalize(
        self,
        status: TickRecordStatus = TickRecordStatus.COMMITTED,
        error: str | None = None,
    ) -> TickRecord:
        """Finalize the current tick record.

        Sets the final status and clears the active record reference.
        Returns the finalized record.
        """
        if self._current_record is None:
            raise RuntimeError("No active tick record to finalize")
        self._current_record.status = status
        self._current_record.error = error
        record = self._current_record
        self._current_record = None
        return record

    @property
    def records(self) -> list[TickRecord]:
        """All tick records (read-only copy)."""
        return list(self._records)

    def for_tick(self, tick: int) -> TickRecord | None:
        """Get the record for a specific tick."""
        for r in self._records:
            if r.tick == tick:
                return r
        return None

    def last(self, n: int = 1) -> list[TickRecord]:
        """Get the last N records."""
        return list(self._records[-n:])

    def __len__(self) -> int:
        return len(self._records)

    def __repr__(self) -> str:
        return f"TickJournal({len(self._records)} records)"
