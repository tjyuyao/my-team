"""Tests for private workspace and file operations.

Per KANBAN tasks: 2026-08-17-private-workspace, 2026-08-17-file-ops
"""

import pytest

from my_team.file_ops import FileOps, FileOpsAuditLog
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


# ---------------------------------------------------------------------------
# FileOps
# ---------------------------------------------------------------------------

class TestFileOpsRead:
    def test_read_own_file(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.initialize_agent("agent.test")
        (home / "workspace" / "data.md").write_text("hello")

        ops = FileOps(private_store=store)
        result = ops.read("agent.test", "workspace/data.md")

        assert result.success
        assert result.data == "hello"

    def test_read_nonexistent_file(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        ops = FileOps(private_store=store)
        result = ops.read("agent.test", "workspace/missing.md")

        assert not result.success
        assert "not found" in result.error.lower()

    def test_read_directory_fails(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        ops = FileOps(private_store=store)
        result = ops.read("agent.test", "workspace")

        assert not result.success
        assert "directory" in result.error.lower()

    def test_read_other_agent_denied(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home_a = store.initialize_agent("agent.a")
        store.initialize_agent("agent.b")
        (home_a / "workspace" / "secret.md").write_text("private")

        ops = FileOps(private_store=store)
        result = ops.read("agent.b", "../agent.a/workspace/secret.md")

        assert not result.success
        assert "denied" in result.error.lower()


class TestFileOpsWrite:
    def test_write_own_file(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        ops = FileOps(private_store=store)
        result = ops.write("agent.test", "workspace/output.md", "content here")

        assert result.success
        output_path = store.agent_home("agent.test") / "workspace" / "output.md"
        assert output_path.read_text() == "content here"

    def test_write_creates_parent_dirs(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        ops = FileOps(private_store=store)
        result = ops.write("agent.test", "workspace/sub/deep/file.md", "nested")

        assert result.success
        assert (store.agent_home("agent.test") / "workspace" / "sub" / "deep" / "file.md").exists()

    def test_write_overwrites_existing(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.initialize_agent("agent.test")
        (home / "workspace" / "file.md").write_text("old")

        ops = FileOps(private_store=store)
        result = ops.write("agent.test", "workspace/file.md", "new")

        assert result.success
        assert (home / "workspace" / "file.md").read_text() == "new"

    def test_write_other_agent_denied(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.a")
        store.initialize_agent("agent.b")

        ops = FileOps(private_store=store)
        result = ops.write("agent.b", "../agent.a/workspace/hack.md", "pwned")

        assert not result.success
        assert "denied" in result.error.lower()

    def test_write_size_limit(self, tmp_path):
        config = PrivateStoreConfig(
            base_path=str(tmp_path),
            max_file_size_bytes=100,
        )
        store = PrivateStore(config)
        store.initialize_agent("agent.test")

        ops = FileOps(private_store=store)
        result = ops.write("agent.test", "workspace/big.md", "x" * 200)

        assert not result.success
        assert "exceeds" in result.error.lower()


class TestFileOpsLs:
    def test_ls_own_workspace(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.initialize_agent("agent.test")
        (home / "workspace" / "a.md").write_text("a")
        (home / "workspace" / "b.md").write_text("bb")

        ops = FileOps(private_store=store)
        result = ops.ls("agent.test", "workspace")

        assert result.success
        names = [e["name"] for e in result.data]
        assert "a.md" in names
        assert "b.md" in names

    def test_ls_nonexistent_dir(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        ops = FileOps(private_store=store)
        result = ops.ls("agent.test", "nonexistent")

        assert not result.success

    def test_ls_file_not_dir(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.initialize_agent("agent.test")
        (home / "workspace" / "file.md").write_text("x")

        ops = FileOps(private_store=store)
        result = ops.ls("agent.test", "workspace/file.md")

        assert not result.success
        assert "not a directory" in result.error.lower()

    def test_ls_other_agent_denied(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.a")
        store.initialize_agent("agent.b")

        ops = FileOps(private_store=store)
        result = ops.ls("agent.b", "../agent.a/workspace")

        assert not result.success


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

class TestFileOpsAudit:
    def test_read_logged(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.initialize_agent("agent.test")
        (home / "workspace" / "file.md").write_text("data")

        audit = FileOpsAuditLog()
        ops = FileOps(private_store=store, audit_log=audit)

        ops.read("agent.test", "workspace/file.md")

        assert len(audit) == 1
        assert audit.entries[0].operation == "read"
        assert audit.entries[0].success is True

    def test_write_logged(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.test")

        audit = FileOpsAuditLog()
        ops = FileOps(private_store=store, audit_log=audit)

        ops.write("agent.test", "workspace/file.md", "content")

        assert len(audit) == 1
        assert audit.entries[0].operation == "write"

    def test_denied_access_logged(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.a")
        store.initialize_agent("agent.b")

        audit = FileOpsAuditLog()
        ops = FileOps(private_store=store, audit_log=audit)

        ops.read("agent.b", "../agent.a/workspace/secret.md")

        assert len(audit) == 1
        assert audit.entries[0].success is False
        assert "denied" in audit.entries[0].error.lower()

    def test_audit_records_tick(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        home = store.initialize_agent("agent.test")
        (home / "workspace" / "f.md").write_text("x")

        audit = FileOpsAuditLog()
        ops = FileOps(private_store=store, audit_log=audit)

        ops.read("agent.test", "workspace/f.md", tick=5)

        assert audit.entries[0].tick == 5

    def test_audit_for_agent(self, tmp_path):
        store = PrivateStore(PrivateStoreConfig(base_path=str(tmp_path)))
        store.initialize_agent("agent.a")
        store.initialize_agent("agent.b")
        home_a = store.agent_home("agent.a")
        (home_a / "workspace" / "f.md").write_text("x")

        audit = FileOpsAuditLog()
        ops = FileOps(private_store=store, audit_log=audit)

        ops.read("agent.a", "workspace/f.md")
        ops.read("agent.b", "../agent.a/workspace/f.md")  # denied via traversal

        assert len(audit.for_agent("agent.a")) == 1
        assert len(audit.for_agent("agent.b")) == 1
