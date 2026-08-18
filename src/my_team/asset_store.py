"""AssetStore: content-addressed binary asset storage (SPEC §7.4, T10).

Design:
- Content addressing (sha256 of bytes): same bytes → same key. This
  makes assets IMMUTABLE and self-deduplicating — an identity collision
  is impossible by construction.
- put/get/stat only; no rename/delete (immutable objects; dead objects
  are GC'd by policy, out of scope).
- private_transfer / shared_kb assets are referenced via AttachmentRef
  (SPEC §4.3): {ref_type, path, version, hash, size, mime} — Email
  attachments reference AssetStore objects or SharedKB entries instead
  of copying payloads (T8b wires the email side).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_MIME = "application/octet-stream"


class AssetNotFoundError(KeyError):
    """Raised when get/stat is asked for a hash the store does not hold."""

    def __init__(self, sha256: str) -> None:
        self.sha256 = sha256
        super().__init__(f"Asset not found: {sha256}")


@dataclass(frozen=True)
class AssetMeta:
    """Metadata for a stored asset."""

    sha256: str
    size: int
    mime: str
    stored_at_tick: int = 0


class AttachmentRef(BaseModel):
    """Reference to a shared payload (SPEC §4.3 — 大内容只存引用，不复制).

    ref_type: 'asset' (AssetStore object) | 'shared_kb' (KB entry) |
              'private_transfer' (reserved for future cross-agent refs).
    """

    ref_type: str = Field(description="'asset' | 'shared_kb' | 'private_transfer'")
    path: str = Field(default="", description="Path/name of the referenced object")
    version: int = Field(default=0, description="Version at reference time")
    hash: str = Field(default="", description="Content hash (sha256)")
    size: int = Field(default=0, description="Payload size in bytes")
    mime: str = Field(default=DEFAULT_MIME, description="Content type")

    @classmethod
    def from_asset(cls, meta: AssetMeta, path: str = "") -> "AttachmentRef":
        """Build a reference to a stored asset."""
        return cls(
            ref_type="asset",
            path=path or meta.sha256,
            hash=meta.sha256,
            size=meta.size,
            mime=meta.mime,
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AssetStore:
    """Content-addressed binary asset store (in-memory, T10).

    Persistence (SQLite) and cross-agent transfer are future work — the
    current contract is in-memory put/get/stat with content addressing.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}      # sha256 → payload
        self._meta: dict[str, AssetMeta] = {}    # sha256 → metadata
        self._counter = 0

    def put(
        self,
        data: bytes,
        mime: str = DEFAULT_MIME,
        tick: int = 0,
    ) -> AssetMeta:
        """Store bytes; returns metadata. Idempotent per content —
        re-putting identical bytes returns the same hash."""
        digest = _sha256(data)
        if digest not in self._meta:
            self._counter += 1
            self._blobs[digest] = data
            self._meta[digest] = AssetMeta(
                sha256=digest,
                size=len(data),
                mime=mime,
                stored_at_tick=tick,
            )
        return self._meta[digest]

    def get(self, sha256: str) -> bytes:
        """Fetch raw bytes by content hash."""
        if sha256 not in self._blobs:
            raise AssetNotFoundError(sha256)
        return self._blobs[sha256]

    def stat(self, sha256: str) -> AssetMeta:
        """Fetch metadata (no payload)."""
        try:
            return self._meta[sha256]
        except KeyError:
            raise AssetNotFoundError(sha256) from None

    def ref(self, sha256: str, path: str = "") -> AttachmentRef:
        """Convenience: a validated AttachmentRef for a stored asset."""
        return AttachmentRef.from_asset(self.stat(sha256), path=path)

    def contains(self, sha256: str) -> bool:
        return sha256 in self._meta

    def __len__(self) -> int:
        return len(self._meta)

    def __contains__(self, sha256: str) -> bool:
        return sha256 in self._meta

    # -- serialization (in-memory state for persistence, if needed) ---------

    def snapshot(self) -> dict[str, Any]:
        import base64
        return {
            "blobs": {
                h: base64.b64encode(b).decode("ascii")
                for h, b in self._blobs.items()
            },
            "meta": {
                h: {
                    "sha256": m.sha256,
                    "size": m.size,
                    "mime": m.mime,
                    "stored_at_tick": m.stored_at_tick,
                }
                for h, m in self._meta.items()
            },
        }

    def restore(self, payload: dict[str, Any]) -> None:
        import base64
        self._blobs = {
            h: base64.b64decode(b)
            for h, b in payload.get("blobs", {}).items()
        }
        self._meta = {
            h: AssetMeta(**m)
            for h, m in payload.get("meta", {}).items()
        }
        self._counter = len(self._meta)
