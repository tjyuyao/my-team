"""Basic file operation tools for agents: read, write, ls.

Per SPEC §4.1, §4.2, §4.3:
- All agents can use read, write, ls on their own private workspace
- Tool calls must pass permission checks
- All tool calls are audit-logged
- Write size and storage limits are enforced
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from my_team.private_store import (
    AccessDeniedError,
    PrivateStore,
    PrivateStoreConfig,
)


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


class FileOps:
    """File operation tools for agents.

    Provides read, write, and ls operations scoped to each agent's
    private workspace with access control enforcement.
    """

    def __init__(
        self,
        private_store: PrivateStore | None = None,
        audit_log: FileOpsAuditLog | None = None,
    ) -> None:
        self._store = private_store if private_store is not None else PrivateStore()
        self._audit_log = audit_log if audit_log is not None else FileOpsAuditLog()

    @property
    def store(self) -> PrivateStore:
        return self._store

    @property
    def audit_log(self) -> FileOpsAuditLog:
        return self._audit_log

    def read(self, agent_id: str, relative_path: str, tick: int | None = None) -> FileOpResult:
        """Read a file from the agent's private workspace.

        Args:
            agent_id: The agent performing the read.
            relative_path: Path relative to agent's home directory.
            tick: Optional simulation tick for audit.

        Returns:
            FileOpResult with file content or error.
        """
        try:
            resolved = self._store.resolve_path(agent_id, relative_path)

            if not resolved.exists():
                return FileOpResult(
                    success=False,
                    agent_id=agent_id,
                    operation="read",
                    path=relative_path,
                    error=f"File not found: {relative_path}",
                )

            if resolved.is_dir():
                return FileOpResult(
                    success=False,
                    agent_id=agent_id,
                    operation="read",
                    path=relative_path,
                    error=f"Is a directory: {relative_path}",
                )

            content = resolved.read_text(encoding="utf-8")
            result = FileOpResult(
                success=True,
                agent_id=agent_id,
                operation="read",
                path=relative_path,
                data=content,
                metadata={"size_bytes": len(content.encode("utf-8"))},
            )

            self._audit_log.record(FileOpsAuditEntry(
                agent_id=agent_id,
                operation="read",
                path=relative_path,
                success=True,
                tick=tick,
            ))
            return result

        except AccessDeniedError as e:
            entry = FileOpsAuditEntry(
                agent_id=agent_id,
                operation="read",
                path=relative_path,
                success=False,
                error=str(e),
                tick=tick,
            )
            self._audit_log.record(entry)
            return FileOpResult(
                success=False,
                agent_id=agent_id,
                operation="read",
                path=relative_path,
                error=str(e),
            )

    def write(
        self,
        agent_id: str,
        relative_path: str,
        content: str,
        tick: int | None = None,
    ) -> FileOpResult:
        """Write a file to the agent's private workspace.

        Args:
            agent_id: The agent performing the write.
            relative_path: Path relative to agent's home directory.
            content: File content to write.
            tick: Optional simulation tick for audit.

        Returns:
            FileOpResult with success status or error.
        """
        try:
            resolved = self._store.resolve_path(agent_id, relative_path)

            # Check file size limit
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > self._store._config.max_file_size_bytes:
                error = (
                    f"Write rejected: {len(content_bytes)} bytes exceeds "
                    f"limit of {self._store._config.max_file_size_bytes} bytes"
                )
                self._audit_log.record(FileOpsAuditEntry(
                    agent_id=agent_id,
                    operation="write",
                    path=relative_path,
                    success=False,
                    error=error,
                    tick=tick,
                ))
                return FileOpResult(
                    success=False,
                    agent_id=agent_id,
                    operation="write",
                    path=relative_path,
                    error=error,
                )

            # Check storage quota
            current_usage = self._store.get_storage_usage(agent_id)
            if resolved.exists():
                current_usage -= resolved.stat().st_size
            if current_usage + len(content_bytes) > self._store._config.max_storage_bytes:
                error = (
                    f"Write rejected: storage quota exceeded "
                    f"({current_usage + len(content_bytes)} / "
                    f"{self._store._config.max_storage_bytes} bytes)"
                )
                self._audit_log.record(FileOpsAuditEntry(
                    agent_id=agent_id,
                    operation="write",
                    path=relative_path,
                    success=False,
                    error=error,
                    tick=tick,
                ))
                return FileOpResult(
                    success=False,
                    agent_id=agent_id,
                    operation="write",
                    path=relative_path,
                    error=error,
                )

            # Ensure parent directory exists
            resolved.parent.mkdir(parents=True, exist_ok=True)

            resolved.write_text(content, encoding="utf-8")
            result = FileOpResult(
                success=True,
                agent_id=agent_id,
                operation="write",
                path=relative_path,
                metadata={"size_bytes": len(content_bytes)},
            )

            self._audit_log.record(FileOpsAuditEntry(
                agent_id=agent_id,
                operation="write",
                path=relative_path,
                success=True,
                tick=tick,
            ))
            return result

        except AccessDeniedError as e:
            self._audit_log.record(FileOpsAuditEntry(
                agent_id=agent_id,
                operation="write",
                path=relative_path,
                success=False,
                error=str(e),
                tick=tick,
            ))
            return FileOpResult(
                success=False,
                agent_id=agent_id,
                operation="write",
                path=relative_path,
                error=str(e),
            )

    def ls(self, agent_id: str, relative_path: str = ".", tick: int | None = None) -> FileOpResult:
        """List directory contents in the agent's private workspace.

        Args:
            agent_id: The agent performing the ls.
            relative_path: Directory path relative to agent's home.
            tick: Optional simulation tick for audit.

        Returns:
            FileOpResult with directory listing or error.
        """
        try:
            resolved = self._store.resolve_path(agent_id, relative_path)

            if not resolved.exists():
                return FileOpResult(
                    success=False,
                    agent_id=agent_id,
                    operation="ls",
                    path=relative_path,
                    error=f"Directory not found: {relative_path}",
                )

            if not resolved.is_dir():
                return FileOpResult(
                    success=False,
                    agent_id=agent_id,
                    operation="ls",
                    path=relative_path,
                    error=f"Not a directory: {relative_path}",
                )

            entries = []
            for item in sorted(resolved.iterdir()):
                entry_type = "dir" if item.is_dir() else "file"
                entry: dict[str, Any] = {
                    "name": item.name,
                    "type": entry_type,
                }
                if item.is_file():
                    entry["size_bytes"] = item.stat().st_size
                entries.append(entry)

            result = FileOpResult(
                success=True,
                agent_id=agent_id,
                operation="ls",
                path=relative_path,
                data=entries,
                metadata={"count": len(entries)},
            )

            self._audit_log.record(FileOpsAuditEntry(
                agent_id=agent_id,
                operation="ls",
                path=relative_path,
                success=True,
                tick=tick,
            ))
            return result

        except AccessDeniedError as e:
            self._audit_log.record(FileOpsAuditEntry(
                agent_id=agent_id,
                operation="ls",
                path=relative_path,
                success=False,
                error=str(e),
                tick=tick,
            ))
            return FileOpResult(
                success=False,
                agent_id=agent_id,
                operation="ls",
                path=relative_path,
                error=str(e),
            )
