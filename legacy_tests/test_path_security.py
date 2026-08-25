"""Tests for path security hardening: symlinks, traversal, absolute paths.

Covers review gap: basic path security (SPEC §15.1).

NOTE: These tests verify static path checks. True TOCTOU resistance
(replacing a symlink between check and open) requires atomic file
operations (openat, O_NOFOLLOW) and is not yet implemented.
"""

import tempfile
from pathlib import Path

import pytest

from my_team.private_store import (
    AccessDeniedError,
    PrivateStore,
    PrivateStoreConfig,
    _is_under_path,
)


@pytest.fixture
def store_with_agents():
    """Create a temporary store with two agents."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = PrivateStoreConfig(base_path=tmpdir)
        store = PrivateStore(config)
        store.initialize_agent("agent.alice")
        store.initialize_agent("agent.bob")
        yield store, Path(tmpdir)


class TestPathTraversal:
    def test_dotdot_traversal_denied(self, store_with_agents):
        store, _ = store_with_agents
        with pytest.raises(AccessDeniedError):
            store.resolve_path("agent.alice", "../../etc/passwd")

    def test_deep_dotdot_traversal_denied(self, store_with_agents):
        store, _ = store_with_agents
        with pytest.raises(AccessDeniedError):
            store.resolve_path("agent.alice", "workspace/../../../../etc/passwd")

    def test_valid_relative_path_allowed(self, store_with_agents):
        store, _ = store_with_agents
        result = store.resolve_path("agent.alice", "workspace/test.txt")
        assert "agent.alice" in str(result)
        assert "workspace" in str(result)

    def test_valid_subdir_path_allowed(self, store_with_agents):
        store, _ = store_with_agents
        result = store.resolve_path("agent.alice", "inbox/email.txt")
        assert "agent.alice" in str(result)
        assert "inbox" in str(result)


class TestSymlinkSecurity:
    def test_symlink_to_sibling_denied(self, store_with_agents):
        """Symlink pointing to another agent's workspace is denied."""
        store, base = store_with_agents
        alice_ws = base / "agent.alice" / "workspace"
        bob_ws = base / "agent.bob" / "workspace"

        # Create a file in Bob's workspace
        (bob_ws / "secret.txt").write_text("secret")

        # Create symlink in Alice's workspace pointing to Bob's file
        symlink = alice_ws / "sneaky_link"
        try:
            symlink.symlink_to(bob_ws / "secret.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        # resolve_path should deny access
        with pytest.raises(AccessDeniedError):
            store.resolve_path("agent.alice", "workspace/sneaky_link")

    def test_symlink_to_etc_denied(self, store_with_agents):
        """Symlink pointing to /etc is denied."""
        store, base = store_with_agents
        alice_ws = base / "agent.alice" / "workspace"

        symlink = alice_ws / "etc_link"
        try:
            symlink.symlink_to("/etc/passwd")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        with pytest.raises(AccessDeniedError):
            store.resolve_path("agent.alice", "workspace/etc_link")

    def test_nested_symlink_denied(self, store_with_agents):
        """Nested symlink (symlink → symlink → outside) is denied."""
        store, base = store_with_agents
        alice_ws = base / "agent.alice" / "workspace"
        bob_ws = base / "agent.bob" / "workspace"

        (bob_ws / "secret.txt").write_text("secret")

        try:
            link1 = alice_ws / "link1"
            link1.symlink_to(bob_ws / "secret.txt")
            link2 = alice_ws / "link2"
            link2.symlink_to(link1)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        with pytest.raises(AccessDeniedError):
            store.resolve_path("agent.alice", "workspace/link2")


class TestAbsolutePaths:
    def test_absolute_path_denied(self, store_with_agents):
        """Absolute path is resolved relative to home, but escape is caught."""
        store, _ = store_with_agents
        # Absolute path outside workspace
        with pytest.raises(AccessDeniedError):
            store.resolve_path("agent.alice", "/etc/passwd")

    def test_absolute_path_within_workspace(self, store_with_agents):
        """Absolute path that happens to be within workspace is allowed."""
        store, base = store_with_agents
        agent_home = base / "agent.alice"
        # Pass absolute path that is within workspace
        result = store.resolve_path("agent.alice", str(agent_home / "workspace" / "file.txt"))
        assert "agent.alice" in str(result)


class TestCheckAccess:
    def test_access_own_workspace(self, store_with_agents):
        store, base = store_with_agents
        assert store.check_access("agent.alice", base / "agent.alice" / "workspace")

    def test_access_other_workspace_denied(self, store_with_agents):
        store, base = store_with_agents
        assert not store.check_access("agent.alice", base / "agent.bob" / "workspace")

    def test_access_sibling_prefix(self, store_with_agents):
        """Ensure /private/agent.alice doesn't match /private/agent.alice_attacker."""
        store, base = store_with_agents
        # Create a sibling directory with a similar prefix
        sibling = base / "agent.alice_attacker"
        sibling.mkdir()
        assert not store.check_access("agent.alice", sibling)


class TestIsUnderPath:
    def test_same_path(self):
        assert _is_under_path(Path("/a/b"), Path("/a/b"))

    def test_child_path(self):
        assert _is_under_path(Path("/a/b/c"), Path("/a/b"))

    def test_sibling_not_under(self):
        assert not _is_under_path(Path("/a/bb"), Path("/a/b"))

    def test_parent_not_under(self):
        assert not _is_under_path(Path("/a"), Path("/a/b"))

    def test_different_root(self):
        assert not _is_under_path(Path("/x/y"), Path("/a/b"))
