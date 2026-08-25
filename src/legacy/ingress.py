"""Ingress transport layer (T9, v0.10 边界).

Direction-neutral inbound side of the transport layer (SPEC §8.1):

- ``IngressEvent`` is the unified envelope carrying an external platform
  event into the kernel. All platforms collapse to this shape; no
  platform-specific semantics leak into the kernel.
- ``IngressBuffer`` accepts events between ticks, deduplicates on the
  persistent ``(source, external_id)`` key (survives restart), and
  exposes them to the Ingest phase. An event is NOT ack'ed until it is
  durably persisted (at-least-once, no loss on crash between receive
  and ack).

The Ingest phase consumes buffered events and may wake related agents
("an event has arrived") WITHOUT deciding a downstream object — that
mapping (IngressEvent -> ProcessInstance) belongs to v0.11 E1. Inbound
receipts that resolve to an outbound op (via an Integration's receipt
assertion) wake the owning agent through the existing wait/wake path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class IngressPriority(str, Enum):
    """Ingress event priority hints (kernel-agnostic)."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class IngressEvent(BaseModel):
    """Unified inbound envelope from an external platform (SPEC §8.1)."""

    source: str = Field(description="Platform name, e.g. 'douyin'/'taobao'")
    external_id: str = Field(description="Platform-side unique id")
    event_type: str = Field(description="Platform event type")
    occurred_at: str = Field(description="Wall-clock occurrence time")
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(
        default="",
        description="Deduplication key (defaults to (source, external_id))",
    )
    priority: IngressPriority = Field(default=IngressPriority.NORMAL)

    @property
    def dedup_key(self) -> tuple[str, str]:
        """Persistent dedup key — (source, external_id)."""
        return (self.source, self.external_id)


class IngressStatus(str, Enum):
    """Unacked (not yet durably persisted) vs acked."""

    PENDING = "pending"      # received, not yet durably persisted
    PERSISTED = "persisted"  # durably stored; ready for Ingest consumption


@dataclass
class IngressBuffer:
    """Inbound buffer with persistent dedup and ack-after-persist.

    Events arrive between ticks (via ``receive``) and are consumed by
    the Ingest phase (``drain``). Persistence is a pluggable callback:
    tests may supply an in-memory store; production supplies a durable
    one. An event transitions PENDING -> PERSISTED only after the
    durability callback returns successfully; only then may the platform
    be ack'ed.
    """

    persist_cb: Any = field(default=None)

    def __init__(self, persist_cb: Any = None) -> None:
        self._events: dict[tuple[str, str], IngressEvent] = {}
        self._acked: set[tuple[str, str]] = set()
        # Cross-restart seen set: (source, external_id) — if a durable
        # snapshot is loaded, these keys are never re-ingested.
        self._seen: set[tuple[str, str]] = set()
        self._persist_cb = persist_cb

    def restore_seen(self, keys: list[tuple[str, str]]) -> None:
        """Load the persistent dedup set (cross-restart)."""
        self._seen = set(keys or ())

    def seen_snapshot(self) -> list[tuple[str, str]]:
        """Persisted dedup keys for cross-restart replay."""
        return sorted(self._seen)

    def receive(self, event: IngressEvent) -> bool:
        """Accept an inbound event if it is not a duplicate.

        Returns True if newly accepted (not a dup), False if it was a
        duplicate (seen before) and is dropped silently.

        An event is 'seen' immediately so that in-flight duplicates
        within the same run are also rejected even before persistence.
        """
        key = event.dedup_key
        if key in self._seen:
            return False
        self._seen.add(key)
        self._events[key] = event
        return True

    def persist(self) -> None:
        """Persist all PENDING-arrived events before ack.

        An event is only eligible for ack once it has been durably
        recorded. If no persist callback is configured, durability is
        assumed in-memory (test mode).
        """
        if self._persist_cb is None:
            for key, ev in self._events.items():
                self._acked.add(key)
            return
        for key, ev in self._events.items():
            if key in self._acked:
                continue
            self._persist_cb(ev)
            self._acked.add(key)

    def drain(self) -> list[IngressEvent]:
        """Consume all persisted events for this Ingest phase (FIFO order)."""
        events = list(self._events.values())
        self._events.clear()
        return events

    def is_acked(self, key: tuple[str, str]) -> bool:
        return key in self._acked

    def pending_count(self) -> int:
        return len(self._events)

    # ------------------------------------------------------------------
    # Durable snapshot helpers (used by the persistence layer)


def snapshot_ingress_buffer(buf: IngressBuffer) -> dict[str, Any]:
    """Serialize an IngressBuffer's durable state for persistence."""
    return {
        "seen": [list(k) for k in buf.seen_snapshot()],
        "pending": [
            ev.model_dump(mode="json")
            for k, ev in list(buf._events.items())
            if k not in buf._acked
        ],
    }


def restore_ingress_buffer(
    data: dict[str, Any] | None,
    persist_cb: Any = None,
) -> IngressBuffer:
    """Reconstruct an IngressBuffer from a persisted snapshot."""
    buf = IngressBuffer(persist_cb=persist_cb)
    data = data or {}
    keys = [tuple(k) for k in data.get("seen", []) if isinstance(k, list)]
    buf.restore_seen(keys)
    for raw in data.get("pending", []):
        try:
            ev = IngressEvent(**raw)
        except Exception:
            continue
        buf._events[ev.dedup_key] = ev
    # Pending-but-unacked events are re-seen to prevent double-ingest.
    for k in list(buf._events.keys()):
        buf._seen.add(k)
    return buf
