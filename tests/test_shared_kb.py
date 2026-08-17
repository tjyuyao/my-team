"""Tests for Phase 3: Shared knowledge base.

Covers: permissions, version control, mutex locks, shared KB, audit log.
"""

import pytest

from my_team.audit import AuditEventType, AuditLog
from my_team.shared_kb import (
    LockConflictError,
    LockManager,
    LockStatus,
    PermissionEngine,
    PermissionRule,
    SharedKB,
    SharedKBWriteError,
    VersionConflictError,
    VersionControl,
)

# ---------------------------------------------------------------------------
# Permission Engine
# ---------------------------------------------------------------------------

class TestPermissionEngine:
    def test_exact_match(self):
        engine = PermissionEngine([
            PermissionRule(scope="project/research/report.md", principal="agent.a", allow=["read"]),
        ])
        assert engine.check("agent.a", "project/research/report.md", "read")
        assert not engine.check("agent.a", "project/research/report.md", "write")

    def test_wildcard_match(self):
        engine = PermissionEngine([
            PermissionRule(
                scope="project/research/*",
                principal="agent.a",
                allow=["read", "write"],
            ),
        ])
        assert engine.check("agent.a", "project/research/report.md", "read")
        assert engine.check("agent.a", "project/research/data.csv", "write")
        assert not engine.check("agent.a", "project/planning/plan.md", "read")

    def test_directory_prefix_match(self):
        engine = PermissionEngine([
            PermissionRule(scope="project/research", principal="agent.a", allow=["read"]),
        ])
        assert engine.check("agent.a", "project/research/file.md", "read")

    def test_no_match(self):
        engine = PermissionEngine([
            PermissionRule(scope="project/research/*", principal="agent.a", allow=["read"]),
        ])
        assert not engine.check("agent.b", "project/research/file.md", "read")

    def test_get_allowed_ops(self):
        engine = PermissionEngine([
            PermissionRule(scope="project/*", principal="agent.a", allow=["read", "list"]),
            PermissionRule(scope="project/research/*", principal="agent.a", allow=["write"]),
        ])
        ops = engine.get_allowed_ops("agent.a", "project/research/report.md")
        assert ops == {"read", "list", "write"}

    def test_multiple_rules_same_path(self):
        engine = PermissionEngine([
            PermissionRule(scope="shared/*", principal="agent.a", allow=["read"]),
            PermissionRule(scope="shared/*", principal="agent.a", allow=["write"]),
        ])
        assert engine.check("agent.a", "shared/data.md", "read")
        assert engine.check("agent.a", "shared/data.md", "write")

    def test_add_rule(self):
        engine = PermissionEngine()
        engine.add_rule(PermissionRule(scope="test/*", principal="agent.x", allow=["read"]))
        assert engine.check("agent.x", "test/file.md", "read")
        assert not engine.check("agent.x", "test/file.md", "write")


# ---------------------------------------------------------------------------
# Version Control
# ---------------------------------------------------------------------------

class TestVersionControl:
    def test_initial_version(self):
        vc = VersionControl()
        assert vc.get_version("path/a.md") == 0

    def test_increment(self):
        vc = VersionControl()
        info = vc.increment("path/a.md", "agent.a", tick=1)
        assert info.version == 1
        assert info.last_modified_by == "agent.a"
        assert vc.get_version("path/a.md") == 1

    def test_increment_twice(self):
        vc = VersionControl()
        vc.increment("path/a.md", "agent.a")
        info = vc.increment("path/a.md", "agent.b", tick=2)
        assert info.version == 2

    def test_check_version(self):
        vc = VersionControl()
        vc.increment("path/a.md", "agent.a")
        assert vc.check_version("path/a.md", 1)
        assert not vc.check_version("path/a.md", 2)

    def test_assert_version_ok(self):
        vc = VersionControl()
        vc.increment("path/a.md", "agent.a")
        vc.assert_version("path/a.md", 1)  # should not raise

    def test_assert_version_conflict(self):
        vc = VersionControl()
        vc.increment("path/a.md", "agent.a")
        with pytest.raises(VersionConflictError) as exc_info:
            vc.assert_version("path/a.md", 99)
        assert exc_info.value.path == "path/a.md"
        assert exc_info.value.expected == 99
        assert exc_info.value.actual == 1


# ---------------------------------------------------------------------------
# Lock Manager
# ---------------------------------------------------------------------------

class TestLockManager:
    def test_acquire_lock(self):
        lm = LockManager()
        lock = lm.acquire("resource/a", "agent.a", current_tick=0)
        assert lock.status == LockStatus.ACTIVE
        assert lock.owner_agent_id == "agent.a"
        assert lm.is_locked("resource/a")

    def test_lock_conflict(self):
        lm = LockManager()
        lm.acquire("resource/a", "agent.a", current_tick=0)
        with pytest.raises(LockConflictError):
            lm.acquire("resource/a", "agent.b", current_tick=1)

    def test_release_lock(self):
        lm = LockManager()
        lock = lm.acquire("resource/a", "agent.a", current_tick=0)
        lm.release("resource/a", "agent.a", lock.lock_token)
        assert not lm.is_locked("resource/a")

    def test_release_wrong_owner(self):
        from my_team.shared_kb import LockTokenError

        lm = LockManager()
        lock = lm.acquire("resource/a", "agent.a", current_tick=0)
        with pytest.raises(LockTokenError):
            lm.release("resource/a", "agent.b", lock.lock_token)
        assert lm.is_locked("resource/a")

    def test_renew_lock(self):
        lm = LockManager()
        lock = lm.acquire("resource/a", "agent.a", current_tick=0, lease_ticks=4)
        assert lock.lease_until_tick == 4
        lm.renew("resource/a", "agent.a", current_tick=2, lock_token=lock.lock_token, lease_ticks=4)
        assert lock.lease_until_tick == 6

    def test_expired_lock_auto_release(self):
        lm = LockManager()
        lm.acquire("resource/a", "agent.a", current_tick=0, lease_ticks=2)
        # Lease expires at tick 2
        expired = lm.check_expired(current_tick=3)
        assert len(expired) == 1
        assert expired[0].status == LockStatus.EXPIRED
        # Can now acquire
        lock = lm.acquire("resource/a", "agent.b", current_tick=3)
        assert lock.owner_agent_id == "agent.b"

    def test_active_locks(self):
        lm = LockManager()
        lm.acquire("a", "agent.x", current_tick=0)
        lm.acquire("b", "agent.y", current_tick=0)
        assert len(lm.active_locks()) == 2

    def test_renew_after_expiry_fails(self):
        lm = LockManager()
        lock = lm.acquire("resource/a", "agent.a", current_tick=0, lease_ticks=2)
        assert not lm.renew("resource/a", "agent.a", current_tick=5, lock_token=lock.lock_token)

    def test_release_stale_holder_prevented(self):
        """Stale-holder attack: A's delayed release cannot remove B's lock."""
        from my_team.shared_kb import LockTokenError

        lm = LockManager()
        lock_a = lm.acquire("resource/a", "agent.a", current_tick=0, lease_ticks=2)
        # A's lease expires at tick 2
        lm.check_expired(current_tick=3)
        lm.acquire("resource/a", "agent.b", current_tick=3)
        # A tries to release with stale token — should raise
        with pytest.raises(LockTokenError):
            lm.release("resource/a", "agent.a", lock_a.lock_token)
        # B's lock is still active
        assert lm.is_locked("resource/a")
        assert lm.get_lock("resource/a").owner_agent_id == "agent.b"

    def test_release_token_mismatch(self):
        from my_team.shared_kb import LockTokenError

        lm = LockManager()
        lm.acquire("resource/a", "agent.a", current_tick=0)
        with pytest.raises(LockTokenError):
            lm.release("resource/a", "agent.a", "forged_token_12345")

    def test_renew_token_mismatch(self):
        from my_team.shared_kb import LockTokenError

        lm = LockManager()
        lm.acquire("resource/a", "agent.a", current_tick=0, lease_ticks=4)
        with pytest.raises(LockTokenError):
            lm.renew("resource/a", "agent.a", current_tick=1, lock_token="forged_token_12345")


# ---------------------------------------------------------------------------
# SharedKB
# ---------------------------------------------------------------------------

class TestSharedKB:
    @pytest.fixture
    def kb(self):
        permissions = PermissionEngine([
            PermissionRule(scope="project/research/*", principal="agent.research",
                           allow=["read", "write", "create", "list", "delete"]),
            PermissionRule(scope="project/research", principal="agent.research",
                           allow=["read", "list"]),
            PermissionRule(scope="project/*", principal="agent.research",
                           allow=["read", "list"]),
            PermissionRule(scope="project/planning/*", principal="agent.planning",
                           allow=["read", "write", "create", "list"]),
            PermissionRule(scope="project/*", principal="agent.root",
                           allow=["read", "list"]),
            PermissionRule(scope="decisions/*", principal="agent.root",
                           allow=["read", "write", "create", "publish"]),
        ])
        return SharedKB(permissions=permissions)

    def test_create_and_read(self, kb):
        resource = kb.create(
            "project/research/report.md",
            agent_id="agent.research",
            content="# Report",
            tick=1,
        )
        assert resource.version == 1
        read = kb.read("project/research/report.md", "agent.research")
        assert read.content == "# Report"

    def test_permission_denied_create(self, kb):
        with pytest.raises(SharedKBWriteError, match="Permission denied"):
            kb.create(
                "project/planning/plan.md",
                agent_id="agent.research",  # research can't write to planning
            )

    def test_write_requires_lock(self, kb):
        kb.create("project/research/data.md", agent_id="agent.research", tick=0)
        with pytest.raises(SharedKBWriteError, match="Must hold lock"):
            kb._apply_committed(
                "project/research/data.md",
                agent_id="agent.research",
                content="updated",
                expected_version=1,
            )

    def test_full_write_cycle(self, kb):
        """Test the complete commit model: create → lock → read → write → unlock."""
        # Create
        kb.create("project/research/data.md", agent_id="agent.research", tick=0)

        # Lock
        lock = kb.locks.acquire("project/research/data.md", "agent.research", current_tick=1)

        # Read
        resource = kb.read("project/research/data.md", "agent.research")
        assert resource.version == 1

        # Write with correct version
        updated = kb._apply_committed(
            "project/research/data.md",
            agent_id="agent.research",
            content="updated data",
            expected_version=1,
            tick=2,
        )
        assert updated.version == 2
        assert updated.content == "updated data"

        # Unlock
        kb.locks.release("project/research/data.md", "agent.research", lock.lock_token)

    def test_version_conflict(self, kb):
        kb.create("project/research/data.md", agent_id="agent.research", tick=0)
        kb.locks.acquire("project/research/data.md", "agent.research", current_tick=1)

        # Write with wrong version
        with pytest.raises(VersionConflictError):
            kb._apply_committed(
                "project/research/data.md",
                agent_id="agent.research",
                content="conflict",
                expected_version=99,  # wrong!
            )

    def test_read_permission_denied(self, kb):
        kb.create("project/research/data.md", agent_id="agent.research", tick=0)
        with pytest.raises(SharedKBWriteError, match="Permission denied"):
            kb.read("project/research/data.md", "agent.planning")

    def test_list_directory(self, kb):
        kb.create("project/research/a.md", agent_id="agent.research", tick=0)
        kb.create("project/research/b.md", agent_id="agent.research", tick=1)
        kb.create("project/planning/c.md", agent_id="agent.planning", tick=2)

        paths = kb.list_dir("project/research", "agent.research")
        assert len(paths) == 2

    def test_delete(self, kb):
        kb.create("project/research/temp.md", agent_id="agent.research", tick=0)
        assert kb.delete("project/research/temp.md", "agent.research")
        resource = kb.get_resource("project/research/temp.md")
        assert resource.exists is False

    def test_read_not_found(self, kb):
        with pytest.raises(SharedKBWriteError, match="not found"):
            kb.read("project/research/nonexistent.md", "agent.research")


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_record_event(self):
        log = AuditLog()
        entry = log.record(AuditEventType.AGENT_CREATED, agent_id="agent.a")
        assert entry.event_type == AuditEventType.AGENT_CREATED
        assert entry.agent_id == "agent.a"
        assert entry.event_id == 1

    def test_record_with_details(self):
        log = AuditLog()
        entry = log.record(
            AuditEventType.EMAIL_SENT,
            agent_id="agent.a",
            tick=5,
            details={"to": "agent.b", "subject": "Hello"},
        )
        assert entry.tick == 5
        assert entry.details["to"] == "agent.b"

    def test_record_failure(self):
        log = AuditLog()
        entry = log.record(
            AuditEventType.PERMISSION_DENIED,
            agent_id="agent.a",
            success=False,
            error="Access denied",
        )
        assert not entry.success
        assert entry.error == "Access denied"

    def test_for_agent(self):
        log = AuditLog()
        log.record(AuditEventType.EMAIL_SENT, agent_id="agent.a")
        log.record(AuditEventType.EMAIL_SENT, agent_id="agent.b")
        log.record(AuditEventType.EMAIL_SENT, agent_id="agent.a")

        assert len(log.for_agent("agent.a")) == 2
        assert len(log.for_agent("agent.b")) == 1

    def test_for_event_type(self):
        log = AuditLog()
        log.record(AuditEventType.EMAIL_SENT)
        log.record(AuditEventType.LOCK_ACQUIRED)
        log.record(AuditEventType.EMAIL_SENT)

        assert len(log.for_event_type(AuditEventType.EMAIL_SENT)) == 2
        assert len(log.for_event_type(AuditEventType.LOCK_ACQUIRED)) == 1

    def test_for_tick(self):
        log = AuditLog()
        log.record(AuditEventType.TICK_ADVANCE, tick=0)
        log.record(AuditEventType.TICK_ADVANCE, tick=1)
        log.record(AuditEventType.TICK_ADVANCE, tick=0)

        assert len(log.for_tick(0)) == 2
        assert len(log.for_tick(1)) == 1

    def test_failures(self):
        log = AuditLog()
        log.record(AuditEventType.TOOL_CALL, success=True)
        log.record(AuditEventType.TOOL_CALL, success=False, error="fail")
        log.record(AuditEventType.TOOL_CALL, success=True)

        assert len(log.failures()) == 1

    def test_since(self):
        log = AuditLog()
        log.record(AuditEventType.CUSTOM)
        log.record(AuditEventType.CUSTOM)
        e3 = log.record(AuditEventType.CUSTOM)
        log.record(AuditEventType.CUSTOM)

        since = log.since(e3.event_id)
        assert len(since) == 1

    def test_last_n(self):
        log = AuditLog()
        for _ in range(5):
            log.record(AuditEventType.CUSTOM)
        last2 = log.last(2)
        assert len(last2) == 2
        assert last2[0].event_id == 4
        assert last2[1].event_id == 5

    def test_sequential_event_ids(self):
        log = AuditLog()
        e1 = log.record(AuditEventType.CUSTOM)
        e2 = log.record(AuditEventType.CUSTOM)
        e3 = log.record(AuditEventType.CUSTOM)
        assert e1.event_id < e2.event_id < e3.event_id

    def test_clear(self):
        log = AuditLog()
        log.record(AuditEventType.CUSTOM)
        log.record(AuditEventType.CUSTOM)
        log.clear()
        assert log.count == 0

