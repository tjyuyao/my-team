"""Private workspace management for agents.

Per SPEC §5:
- Each agent has an isolated private workspace
- Agents cannot access other agents' private spaces
- Directory structure: inbox/, outbox/, workspace/, memory/, task_state/, logs/
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# Default subdirectories for each agent's private space
PRIVATE_SUBDIRS = ["inbox", "outbox", "workspace", "memory", "task_state", "logs"]


def _is_under_path(child: Path, parent: Path) -> bool:
    """Check if child path is strictly under parent (or equal to it).

    Uses Path.is_relative_to when available (Python 3.9+), with a
    fallback that prevents sibling-directory prefix matching.
    """
    if hasattr(child, "is_relative_to"):
        return child.is_relative_to(parent)
    # Fallback: compare resolved strings with trailing separator
    parent_str = str(parent).rstrip("/") + "/"
    child_str = str(child).rstrip("/")
    return child_str == str(parent).rstrip("/") or child_str.startswith(parent_str)


class PrivateStoreConfig(BaseModel):
    """Configuration for the private store."""

    base_path: str = Field(
        default="private",
        description="Base directory for all private workspaces",
    )
    max_file_size_bytes: int = Field(
        default=10 * 1024 * 1024,  # 10 MB
        description="Maximum single file write size",
    )
    max_storage_bytes: int = Field(
        default=512 * 1024 * 1024,  # 512 MB
        description="Maximum total storage per agent",
    )


class AccessDeniedError(Exception):
    """Raised when an agent tries to access another agent's private space."""

    def __init__(self, agent_id: str, target_path: str) -> None:
        self.agent_id = agent_id
        self.target_path = target_path
        super().__init__(
            f"Agent '{agent_id}' denied access to '{target_path}'"
        )


class FileNotFoundError(Exception):
    """Raised when a requested file does not exist."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"File not found: '{path}'")


class PrivateStore:
    """Manages private workspaces for all agents.

    Provides directory initialization, path resolution, and access control.
    """

    def __init__(self, config: PrivateStoreConfig | None = None) -> None:
        self._config = config or PrivateStoreConfig()
        self._base_path = Path(self._config.base_path)
        self._agent_dirs: dict[str, Path] = {}

    @property
    def base_path(self) -> Path:
        return self._base_path

    def agent_home(self, agent_id: str) -> Path:
        """Get the home directory path for an agent."""
        return self._base_path / agent_id

    def agent_exists(self, agent_id: str) -> bool:
        """Check if an agent's workspace has been initialized."""
        return agent_id in self._agent_dirs

    def initialize_agent(self, agent_id: str) -> Path:
        """Create the full private workspace directory structure for an agent.

        Returns the agent's home directory path.
        """
        home = self.agent_home(agent_id)
        for subdir in PRIVATE_SUBDIRS:
            (home / subdir).mkdir(parents=True, exist_ok=True)
        self._agent_dirs[agent_id] = home
        return home

    def initialize_all(self, agent_ids: list[str]) -> dict[str, Path]:
        """Initialize private workspaces for all agents.

        Returns mapping of agent_id -> home directory path.
        """
        result: dict[str, Path] = {}
        for agent_id in agent_ids:
            result[agent_id] = self.initialize_agent(agent_id)
        return result

    def resolve_path(self, agent_id: str, relative_path: str) -> Path:
        """Resolve a relative path within an agent's private space.

        The relative_path is resolved relative to the agent's home directory.
        Path traversal (../), symlinks, and absolute paths are handled:
        - ../ traversal is caught by resolve() + containment check
        - Symlinks are followed by resolve() — if target is outside home, denied
        - Absolute paths are normalized relative to home

        Raises:
            AccessDeniedError: If the resolved path escapes the agent's workspace.
        """
        home = self.agent_home(agent_id)

        # Normalize and resolve, then check it's still under home
        # resolve() follows symlinks, so symlink escapes are caught
        candidate = (home / relative_path).resolve()
        home_resolved = home.resolve()

        # Use Path.is_relative_to for robust containment check (Python 3.9+)
        # Falls back to manual check for older versions
        if not _is_under_path(candidate, home_resolved):
            raise AccessDeniedError(agent_id, relative_path)

        return candidate

    def check_access(self, agent_id: str, target_path: str | Path) -> bool:
        """Check if an agent has access to a given path.

        Access is granted only if the resolved path is within the agent's own workspace.
        Symlinks are resolved before checking.
        """
        target = Path(target_path).resolve()
        home = self.agent_home(agent_id).resolve()
        return _is_under_path(target, home)

    def assert_access(self, agent_id: str, target_path: str | Path) -> None:
        """Assert that an agent has access to a path. Raises on denial."""
        if not self.check_access(agent_id, target_path):
            raise AccessDeniedError(agent_id, str(target_path))

    def get_storage_usage(self, agent_id: str) -> int:
        """Get total storage usage in bytes for an agent's workspace."""
        home = self.agent_home(agent_id)
        if not home.exists():
            return 0
        total = 0
        for f in home.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
        return total

    def list_agents(self) -> list[str]:
        """List all initialized agent IDs."""
        return list(self._agent_dirs.keys())

    def agent_subdirs(self, agent_id: str) -> dict[str, Path]:
        """Get the subdirectory paths for an agent."""
        home = self.agent_home(agent_id)
        return {name: home / name for name in PRIVATE_SUBDIRS}

    def __repr__(self) -> str:
        return f"PrivateStore(base={self._base_path}, agents={len(self._agent_dirs)})"
