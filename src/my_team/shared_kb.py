"""Shared knowledge base: path permissions, mutex locks, version control.

Per SPEC §6:
- Shared storage accessible by multiple agents
- Path-level and operation-level permission control
- Exclusive mutex locks for writes
- Optimistic versioning: each write carries read-time version
- Conflict detection on commit

N1c-1: SharedKB 归位为 Device 子类（SPEC §5.2，N1c 设备适配层）。
设备注册受控 uuid（范围级 DATA + 工具面 TOOL）+ InjectionDecl，
构造签名保持完全兼容（simulation.py 不变）。
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from my_team.devices.base import Device, EntityKind, InjectionDecl

# ---------------------------------------------------------------------------
# Permission model (§6.2)
# ---------------------------------------------------------------------------

class PermissionOp(str, Enum):
    """Operations that can be permitted on shared KB paths."""

    LIST = "list"
    READ = "read"
    CREATE = "create"
    WRITE = "write"
    APPEND = "append"
    RENAME = "rename"
    DELETE = "delete"
    LOCK = "lock"
    UNLOCK = "unlock"
    PUBLISH = "publish"


class PermissionRule(BaseModel):
    """A single permission rule: principal + scope + allowed ops."""

    scope: str = Field(description="Path pattern, e.g. 'project/research/*'")
    principal: str = Field(description="Agent ID this rule applies to")
    allow: list[str] = Field(description="Allowed operations")


class PermissionEngine:
    """Evaluates permission rules against requested operations.

    Supports glob-style path matching (e.g. 'project/research/*').
    """

    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self._rules: list[PermissionRule] = rules or []

    def add_rule(self, rule: PermissionRule) -> None:
        self._rules.append(rule)

    def add_rules(self, rules: list[PermissionRule]) -> None:
        self._rules.extend(rules)

    def check(self, principal: str, path: str, operation: str) -> bool:
        """Check if a principal has permission for an operation on a path.

        Returns True if any matching rule allows the operation.
        """
        for rule in self._rules:
            if rule.principal != principal:
                continue
            if not self._path_matches(path, rule.scope):
                continue
            if operation in rule.allow:
                return True
        return False

    def get_allowed_ops(self, principal: str, path: str) -> set[str]:
        """Get all allowed operations for a principal on a path."""
        allowed: set[str] = set()
        for rule in self._rules:
            if rule.principal != principal:
                continue
            if not self._path_matches(path, rule.scope):
                continue
            allowed.update(rule.allow)
        return allowed

    @staticmethod
    def _path_matches(path: str, scope: str) -> bool:
        """Check if a path matches a scope pattern.

        Supports:
        - Exact match: 'project/research/report.md'
        - Directory prefix: 'project/research/*'
        - Recursive: 'project/**'
        """
        # Normalize: strip leading/trailing slashes
        path = path.strip("/")
        scope = scope.strip("/")

        # Exact match
        if path == scope:
            return True

        # Wildcard matching
        if scope.endswith("*"):
            prefix = scope[:-1]
            return path.startswith(prefix)

        # Check if scope is a directory prefix
        if path.startswith(scope + "/"):
            return True

        return False


# ---------------------------------------------------------------------------
# Version control (§6.3)
# ---------------------------------------------------------------------------

class VersionInfo(BaseModel):
    """Version metadata for a shared KB resource."""

    path: str
    version: int = Field(default=0)
    last_modified_by: str = Field(default="")
    last_modified_at_tick: int = Field(default=0)


class VersionConflictError(Exception):
    """Raised when a write conflicts with expected version."""

    def __init__(self, path: str, expected: int, actual: int) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Version conflict for '{path}': expected {expected}, actual {actual}"
        )


class VersionControl:
    """Manages optimistic versioning for shared KB resources.

    Each resource has a version number. Writers must carry the version
    they read; mismatch causes commit failure.
    """

    def __init__(self) -> None:
        self._versions: dict[str, VersionInfo] = {}

    def get_version(self, path: str) -> int:
        """Get current version of a resource."""
        info = self._versions.get(path)
        return info.version if info else 0

    def get_info(self, path: str) -> VersionInfo | None:
        return self._versions.get(path)

    def check_version(self, path: str, expected_version: int) -> bool:
        """Check if expected version matches current version."""
        return self.get_version(path) == expected_version

    def increment(
        self,
        path: str,
        modified_by: str,
        tick: int = 0,
    ) -> VersionInfo:
        """Increment version after a successful write. Returns new version info."""
        current = self._versions.get(path)
        new_version = (current.version + 1) if current else 1
        info = VersionInfo(
            path=path,
            version=new_version,
            last_modified_by=modified_by,
            last_modified_at_tick=tick,
        )
        self._versions[path] = info
        return info

    def assert_version(self, path: str, expected_version: int) -> None:
        """Assert version matches. Raises VersionConflictError on mismatch."""
        actual = self.get_version(path)
        if actual != expected_version:
            raise VersionConflictError(path, expected_version, actual)

    def all_versions(self) -> dict[str, VersionInfo]:
        return dict(self._versions)


# ---------------------------------------------------------------------------
# Mutex lock manager (§6.3)
# ---------------------------------------------------------------------------

class LockStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"


class LockInfo(BaseModel):
    """A mutex lock on a shared KB resource."""

    lock_id: str
    resource: str
    owner_agent_id: str
    mode: str = Field(default="exclusive")
    acquired_at_tick: int = 0
    lease_until_tick: int = 0
    status: LockStatus = LockStatus.ACTIVE
    lock_token: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Opaque token required for release/renew to prevent stale-holder attacks",
    )


class LockConflictError(Exception):
    """Raised when a lock cannot be acquired."""

    def __init__(self, resource: str, owner: str) -> None:
        self.resource = resource
        self.owner = owner
        super().__init__(
            f"Cannot lock '{resource}': already held by '{owner}'"
        )


class LockTokenError(Exception):
    """Raised when a lock release/renew fails due to token mismatch.

    Prevents stale-holder attacks where a delayed release() could
    accidentally remove a different agent's lock.
    """

    def __init__(self, resource: str, operation: str) -> None:
        self.resource = resource
        self.operation = operation
        super().__init__(
            f"Lock {operation} failed for '{resource}': invalid token"
        )


class LockManager:
    """Manages exclusive mutex locks on shared KB resources.

    Rules per SPEC §6.3:
    - Same resource can have at most one exclusive write lock
    - Lock has lease with timeout
    - Auto-release on lease expiry
    - Agents can renew leases
    """

    def __init__(self, default_lease_ticks: int = 4) -> None:
        self._locks: dict[str, LockInfo] = {}  # resource → LockInfo
        self._lock_counter = 0
        self._default_lease_ticks = default_lease_ticks

    def acquire(
        self,
        resource: str,
        agent_id: str,
        current_tick: int,
        lease_ticks: int | None = None,
    ) -> LockInfo:
        """Acquire an exclusive lock on a resource.

        Raises LockConflictError if resource is already locked.
        """
        lease = lease_ticks or self._default_lease_ticks

        # Check existing lock
        existing = self._locks.get(resource)
        if existing and existing.status == LockStatus.ACTIVE:
            if existing.lease_until_tick > current_tick:
                raise LockConflictError(resource, existing.owner_agent_id)
            # Existing lock has expired, release it
            existing.status = LockStatus.EXPIRED

        self._lock_counter += 1
        lock = LockInfo(
            lock_id=f"lock.{self._lock_counter:06d}",
            resource=resource,
            owner_agent_id=agent_id,
            acquired_at_tick=current_tick,
            lease_until_tick=current_tick + lease,
            status=LockStatus.ACTIVE,
        )
        self._locks[resource] = lock
        return lock

    def release(self, resource: str, agent_id: str, lock_token: str) -> None:
        """Release a lock.

        Requires the correct lock_token to prevent stale-holder attacks.
        Raises LockTokenError if token is invalid or lock not found.
        """
        lock = self._locks.get(resource)
        if lock is None or lock.status != LockStatus.ACTIVE:
            raise LockTokenError(resource, "release")
        if lock.lock_token != lock_token:
            raise LockTokenError(resource, "release")
        if lock.owner_agent_id != agent_id:
            raise LockTokenError(resource, "release")
        lock.status = LockStatus.RELEASED

    def renew(
        self,
        resource: str,
        agent_id: str,
        current_tick: int,
        lock_token: str,
        lease_ticks: int | None = None,
    ) -> bool:
        """Renew a lock's lease.

        Requires the correct lock_token. Returns True if successful,
        False if lock is expired or not found. Raises LockTokenError
        if token is invalid.
        """
        lock = self._locks.get(resource)
        if lock is None or lock.status != LockStatus.ACTIVE:
            return False
        if lock.lock_token != lock_token:
            raise LockTokenError(resource, "renew")
        if lock.owner_agent_id != agent_id:
            return False
        if lock.lease_until_tick <= current_tick:
            return False  # already expired
        lease = lease_ticks or self._default_lease_ticks
        lock.lease_until_tick = current_tick + lease
        return True

    def check_expired(self, current_tick: int) -> list[LockInfo]:
        """Find and mark expired locks."""
        expired: list[LockInfo] = []
        for lock in self._locks.values():
            if (
                lock.status == LockStatus.ACTIVE
                and lock.lease_until_tick <= current_tick
            ):
                lock.status = LockStatus.EXPIRED
                expired.append(lock)
        return expired

    def is_locked(self, resource: str, current_tick: int | None = None) -> bool:
        """Check if a resource is currently locked."""
        lock = self._locks.get(resource)
        if lock is None or lock.status != LockStatus.ACTIVE:
            return False
        if current_tick is not None and lock.lease_until_tick <= current_tick:
            return False
        return True

    def get_lock(self, resource: str) -> LockInfo | None:
        lock = self._locks.get(resource)
        if lock and lock.status == LockStatus.ACTIVE:
            return lock
        return None

    def active_locks(self) -> list[LockInfo]:
        return [lock for lock in self._locks.values() if lock.status == LockStatus.ACTIVE]

    def __len__(self) -> int:
        return len([lock for lock in self._locks.values() if lock.status == LockStatus.ACTIVE])


# ---------------------------------------------------------------------------
# SharedKB: the complete shared knowledge base
# ---------------------------------------------------------------------------

class SharedKBWriteError(Exception):
    """Raised when a shared KB write fails (permission, lock, version)."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Write failed for '{path}': {reason}")


class SharedKBResource(BaseModel):
    """A resource in the shared knowledge base."""

    path: str
    content: str = ""
    version: int = 0
    last_modified_by: str = ""
    last_modified_at_tick: int = 0
    exists: bool = False


class SharedKB(Device):
    """Shared knowledge base with permissions, locking, and versioning.

    Coordinates all three subsystems to provide safe concurrent access
    to shared resources.

    N1c-1 设备归位：继承 Device，构造时注册受控 uuid
    （范围级 DATA + 工具面 TOOL）并声明 InjectionDecl。
    构造签名保持原样（simulation.py 兼容）。
    """

    def __init__(
        self,
        permissions: PermissionEngine | None = None,
        lock_manager: LockManager | None = None,
        version_control: VersionControl | None = None,
        device_id: str | None = None,
    ) -> None:
        # Device 基类初始化
        Device.__init__(self, device_id)
        # NOTE: explicit None checks — LockManager defines __len__,
        # so `lock_manager or LockManager()` would replace a valid
        # empty lock manager with a fresh instance.
        self._permissions = permissions if permissions is not None else PermissionEngine()
        self._locks = lock_manager if lock_manager is not None else LockManager()
        self._versions = version_control if version_control is not None else VersionControl()
        self._resources: dict[str, SharedKBResource] = {}
        # N1c-1：注册设备受控实体
        # 范围级 DATA 实体 — 知识库整体范围，InjectionDecl 引导 bash
        self.kb_scope_id = self.register_entity(
            EntityKind.DATA,
            "shared-kb-scope",
            injection=InjectionDecl(
                content=(
                    "[KB_INSTRUCTION] 知识库（SharedKB）是团队的共享知识空间。\n"
                    "通过 kb_read/kb_list/kb_search 工具读取条目，"
                    "通过 kb_write 工具（STAGED_MUTATION）写入。\n"
                    "写入需先锁定（lock）并携带读取时的版本号（optimistic locking）。"
                ),
                source_tag="[KB_INSTRUCTION]",
            ),
        )
        # 工具面 TOOL 实体 — 采用 uuid5 派生值（adopt 机制）
        from my_team.tool_manifest import builtin_manifests
        _manifests = builtin_manifests()
        self.kb_read_capability = self.register_entity(
            EntityKind.TOOL, "kb_read",
            entity_id=_manifests["kb_read"].capability,
        )
        self.kb_write_capability = self.register_entity(
            EntityKind.TOOL, "kb_write",
            entity_id=_manifests["kb_write"].capability,
        )
        self.kb_list_capability = self.register_entity(
            EntityKind.TOOL, "kb_list",
            entity_id=_manifests["kb_list"].capability,
        )
        self.kb_search_capability = self.register_entity(
            EntityKind.TOOL, "kb_search",
            entity_id=_manifests["kb_search"].capability,
        )

    @property
    def permissions(self) -> PermissionEngine:
        return self._permissions

    @property
    def locks(self) -> LockManager:
        return self._locks

    @property
    def versions(self) -> VersionControl:
        return self._versions

    def create(
        self,
        path: str,
        agent_id: str,
        content: str = "",
        tick: int = 0,
    ) -> SharedKBResource:
        """Create a new resource (permission: 'create')."""
        if not self._permissions.check(agent_id, path, PermissionOp.CREATE):
            raise SharedKBWriteError(path, f"Permission denied: {agent_id} cannot create")

        if path in self._resources and self._resources[path].exists:
            raise SharedKBWriteError(path, "Resource already exists")

        resource = SharedKBResource(
            path=path,
            content=content,
            version=1,
            last_modified_by=agent_id,
            last_modified_at_tick=tick,
            exists=True,
        )
        self._resources[path] = resource
        self._versions.increment(path, agent_id, tick)
        return resource

    def read(
        self,
        path: str,
        agent_id: str,
    ) -> SharedKBResource:
        """Read a resource (permission: 'read')."""
        if not self._permissions.check(agent_id, path, PermissionOp.READ):
            raise SharedKBWriteError(path, f"Permission denied: {agent_id} cannot read")

        resource = self._resources.get(path)
        if resource is None or not resource.exists:
            raise SharedKBWriteError(path, "Resource not found")

        return resource

    def _apply_committed(
        self,
        path: str,
        agent_id: str,
        content: str,
        expected_version: int,
        tick: int = 0,
    ) -> SharedKBResource:
        """Apply a committed KB write (INTERNAL — commit pipeline only).

        Full commit model per SPEC §6.3:
        1. Permission check
        2. Lock check
        3. Version check
        4. Commit

        NOTE: This is an internal method called ONLY by the commit
        pipeline (TransactionBuffer application). Agents must not call
        this directly — they stage KB_WRITE effects via
        TransactionBuffer.stage() and the system validates + applies
        them atomically.
        """
        # 1. Permission check
        if not self._permissions.check(agent_id, path, PermissionOp.WRITE):
            raise SharedKBWriteError(path, f"Permission denied: {agent_id} cannot write")

        # 2. Lock check
        if not self._locks.is_locked(path):
            raise SharedKBWriteError(path, "Must hold lock to write")
        lock = self._locks.get_lock(path)
        if lock and lock.owner_agent_id != agent_id:
            raise SharedKBWriteError(path, f"Lock held by {lock.owner_agent_id}")

        # 3. Version check
        self._versions.assert_version(path, expected_version)

        # 4. Commit
        resource = self._resources.get(path)
        if resource is None:
            resource = SharedKBResource(path=path, exists=True)

        resource.content = content
        resource.version = expected_version + 1
        resource.last_modified_by = agent_id
        resource.last_modified_at_tick = tick
        resource.exists = True

        self._resources[path] = resource
        self._versions.increment(path, agent_id, tick)
        return resource

    def delete(
        self,
        path: str,
        agent_id: str,
    ) -> bool:
        """Delete a resource (permission: 'delete')."""
        if not self._permissions.check(agent_id, path, PermissionOp.DELETE):
            raise SharedKBWriteError(path, f"Permission denied: {agent_id} cannot delete")

        resource = self._resources.get(path)
        if resource is None or not resource.exists:
            return False

        resource.exists = False
        resource.content = ""
        return True

    def list_dir(
        self,
        path: str,
        agent_id: str,
    ) -> list[str]:
        """List resources under a path prefix (permission: 'list')."""
        if not self._permissions.check(agent_id, path, PermissionOp.LIST):
            raise SharedKBWriteError(path, f"Permission denied: {agent_id} cannot list")

        prefix = path.strip("/") + "/"
        return [
            rpath for rpath, res in self._resources.items()
            if res.exists and rpath.startswith(prefix)
        ]

    def search(
        self,
        query: str,
        agent_id: str,
        base_path: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Keyword search over READABLE KB entries (SPEC §7.2, T8a).

        Candidate set = paths under ``base_path`` that exist AND for
        which the agent holds READ permission. Unauthorized entries are
        neither matched nor present in the result — deny-by-default, no
        "exists but not allowed" leak.

        Matching (v1, embedding-ready interface): case-insensitive
        substring on path or content. Returns up to ``limit`` hits as
        metadata + snippet (first 200 chars); full content goes through
        read() — search never returns whole entries.
        """
        q = query.strip().lower()
        if not q:
            return []
        base = base_path.strip("/")
        prefix = base + "/" if base else ""

        hits: list[dict[str, Any]] = []
        for path, res in self._resources.items():
            if not res.exists:
                continue
            # base_path scope: paths under the prefix (or all when empty)
            if base and not path.startswith(prefix):
                continue
            # Permission filter FIRST — unauthorized entries are
            # invisible to the search (SPEC §7.2, deny-by-default).
            if not self._permissions.check(
                agent_id, path, PermissionOp.READ.value,
            ):
                continue
            if q in path.lower() or q in res.content.lower():
                hits.append({
                    "path": path,
                    "version": res.version,
                    "snippet": res.content[:200],
                    "last_modified_by": res.last_modified_by,
                    "last_modified_at_tick": res.last_modified_at_tick,
                })
                if len(hits) >= limit:
                    break
        return hits

    def get_resource(self, path: str) -> SharedKBResource | None:
        """Get a resource directly (for internal use)."""
        return self._resources.get(path)

    def all_paths(self) -> list[str]:
        return [p for p, r in self._resources.items() if r.exists]
