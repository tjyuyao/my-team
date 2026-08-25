"""Tests for private workspace and file operations.

Per KANBAN tasks: 2026-08-17-private-workspace, 2026-08-17-file-ops

NOTE: FileOps class was removed in v0.9 dead-module cleanup.
File operations go through PrivateStore.resolve_path() directly
in Simulation. These tests verify PrivateStore path safety.
"""

import pytest

from my_team.private_store import (
    AccessDeniedError,
    PrivateStore,
    PrivateStoreConfig,
)

# ---------------------------------------------------------------------------
# PrivateStore
# ---------------------------------------------------------------------------

class TestPrivateStore:
    def test_initialize_agent_creates_dirs(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.initialize_agent("agent.root")

        assert home.exists()
        assert (home / "inbox").is_dir()
        assert (home / "outbox").is_dir()
        assert (home / "workspace").is_dir()
        assert (home / "memory").is_dir()
        assert (home / "task_state").is_dir()
        assert (home / "logs").is_dir()

    def test_initialize_all(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        result = store.initialize_all(["agent.root", "agent.research", "agent.planning"])

        assert len(result) == 3
        assert all(p.exists() for p in result.values())
        assert store.list_agents() == ["agent.root", "agent.research", "agent.planning"]

    def test_agent_home(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.agent_home("agent.test")
        assert home == tmp_path / "agent.test"

    def test_agent_exists(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        assert not store.agent_exists("agent.test")
        store.initialize_agent("agent.test")
        assert store.agent_exists("agent.test")

    def test_resolve_path_within_workspace(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        resolved = store.resolve_path("agent.test", "workspace/data.md")
        assert resolved.exists() or str(tmp_path) in str(resolved)

    def test_resolve_path_traversal_blocked(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        with pytest.raises(AccessDeniedError):
            store.resolve_path("agent.test", "../../etc/passwd")

    def test_check_access_own_space(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        home = store.agent_home("agent.test")
        assert store.check_access("agent.test", home / "workspace/file.md")

    def test_check_access_other_agent_denied(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.a")
        store.initialize_agent("agent.b")

        b_home = store.agent_home("agent.b")
        assert not store.check_access("agent.a", b_home / "workspace/file.md")

    def test_get_storage_usage_empty(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")
        assert store.get_storage_usage("agent.test") == 0

    def test_get_storage_usage_with_files(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.initialize_agent("agent.test")
        (home / "workspace" / "file.txt").write_text("hello world")
        assert store.get_storage_usage("agent.test") == 11

    def test_agent_subdirs(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        subdirs = store.agent_subdirs("agent.test")
        assert "inbox" in subdirs
        assert "workspace" in subdirs
        assert "memory" in subdirs
