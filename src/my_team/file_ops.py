"""File operation audit models.

Per SPEC §4.1, §4.2, §4.3:
- Audit models for file operations (read, write, ls)
- Actual file operations go through PrivateStore.resolve_path()
  directly in Simulation — this module provides the audit data model
  used by persistence for serialization.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FileOpResult(BaseModel):
    """Result of a file operation."""

    success: bool = Field(description="Whether the operation succeeded")
    agent_id: str = Field(description="Agent that performed the operation")
    operation: str = Field(description="Operation name: read, write, ls")
    path: str = Field(description="Target path")
    data: Any = Field(default=None, description="Operation output (content, listing, etc.)")
    error: str | None = Field(default=None, description="Error message if failed")
    metadata: dict[str, Any] = Field(default_factory=dict)


class FileOpsAuditEntry(BaseModel):
    """Audit log entry for a file operation."""

    agent_id: str
    operation: str
    path: str
    success: bool
    error: str | None = None
    tick: int | None = None


class FileOpsAuditLog:
    """Append-only audit log for file operations."""

    def __init__(self) -> None:
        self._entries: list[FileOpsAuditEntry] = []

    def record(self, entry: FileOpsAuditEntry) -> None:
        self._entries.append(entry)

    @property
    def entries(self) -> list[FileOpsAuditEntry]:
        return list(self._entries)

    def for_agent(self, agent_id: str) -> list[FileOpsAuditEntry]:
        return [e for e in self._entries if e.agent_id == agent_id]

    def __len__(self) -> int:
        return len(self._entries)
