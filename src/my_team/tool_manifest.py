"""Tool manifest + operation policy (v0.7.0 P1-1).

A ToolManifest is the declarative contract of a tool: what it does,
what side effects it produces, how it may be executed, and under what
constraints. It is the plugin contract unit: a future plugin registry
(v0.8+) is a bundle of manifests + executors; the tick kernel only
needs the manifest to enforce policy — it never inspects tool code.

An OperationPolicy is the deployment-time control surface: an admin
restricts a tool's blast radius (network, filesystem, wall time, output
size, approval) without touching tool code. Deny-by-default: a tool is
allowed only if listed in the policy.

Two-phase validation semantics (SPEC / v0.7.0 plan):
- PreValidate (Phase 6) consults manifest + policy: "is this attempt
  allowed to try?"
- CommitValidate (Phase 8) re-checks live preconditions: "is it still
  committable now?"
The manifest is used in both: PreValidate checks execution_class and
policy gates; CommitValidate cross-checks declared effect_types against
the actual staged effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from my_team.transaction import EffectType


class ToolManifestError(ValueError):
    """Raised when a manifest or policy fails registration validation."""


class ExecutionClass(str, Enum):
    """How a tool may be executed (declared, enforced by the kernel)."""

    PURE = "pure"
    # ^ No side effects, no I/O, deterministic output from input alone.
    READ_ONLY = "read_only"
    # ^ Reads system state (frozen snapshot view), never mutates.
    LOCAL_DETERMINISTIC = "local_deterministic"
    # ^ In-process computation, deterministic, no external effects.
    STAGED_MUTATION = "staged_mutation"
    # ^ Mutation staged as an effect, committed atomically in Commit
    #   phase; reversible via rollback.
    SANDBOXED_PROCESS = "sandboxed_process"
    # ^ External process in an isolated sandbox (read-only mount,
    #   output truncation, timeout). Effects confined to the sandbox.
    EXTERNAL_IRREVERSIBLE = "external_irreversible"
    # ^ External call that cannot be undone; must be declared
    #   irreversible and (typically) requires approval.


class RetryPolicy(str, Enum):
    """Retry policy for tool invocation failures."""

    NONE = "none"
    FIXED = "fixed"
    EXPONENTIAL = "exponential"


# Valid filesystem scope tokens for ToolManifest.filesystem_scopes.
FILESYSTEM_SCOPES = frozenset({
    "none",        # no filesystem access
    "private",     # agent's private workspace only
    "shared-kb",   # shared knowledge base only
    "workspace",   # simulation workspace (sandboxed)
    "system",      # host filesystem (explicitly discouraged)
})


@dataclass(frozen=True)
class ToolManifest:
    """Declarative contract of a tool. Registration validates it."""

    name: str
    version: str
    execution_class: ExecutionClass
    # JSON-Schema-ish dicts (kept as plain dicts: the kernel only needs
    # shape validation, not a full schema engine).
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    effect_types: tuple[EffectType, ...] = ()
    deterministic: bool = True
    idempotent: bool = False
    reversible: bool = True
    requires_network: bool = False
    filesystem_scopes: tuple[str, ...] = ("none",)
    max_runtime_ms: int | None = None
    max_output_bytes: int | None = None
    supports_cancel: bool = False
    requires_approval: bool = False
    retry_policy: RetryPolicy = RetryPolicy.NONE

    def __post_init__(self) -> None:
        errors: list[str] = []

        if not self.name or not self.name.strip():
            errors.append("name must be a non-empty string")
        if not self.version or not self.version.strip():
            errors.append("version must be a non-empty string")
        if not isinstance(self.input_schema, dict):
            errors.append("input_schema must be a dict")
        if not isinstance(self.output_schema, dict):
            errors.append("output_schema must be a dict")
        if not isinstance(self.effect_types, tuple):
            errors.append("effect_types must be a tuple of EffectType")
        for et in self.effect_types:
            if not isinstance(et, EffectType):
                errors.append(f"effect_types contains non-EffectType: {et!r}")
        for scope in self.filesystem_scopes:
            if scope not in FILESYSTEM_SCOPES:
                errors.append(f"invalid filesystem_scope: {scope!r}")
        if self.max_runtime_ms is not None and self.max_runtime_ms < 0:
            errors.append("max_runtime_ms must be >= 0")
        if self.max_output_bytes is not None and self.max_output_bytes < 0:
            errors.append("max_output_bytes must be >= 0")

        # Execution-class coherence.
        if self.execution_class in {ExecutionClass.PURE, ExecutionClass.READ_ONLY}:
            if self.effect_types:
                errors.append(
                    f"{self.execution_class.value} tools cannot declare effect_types"
                )
        if self.execution_class is ExecutionClass.STAGED_MUTATION:
            if not self.effect_types:
                errors.append(
                    "STAGED_MUTATION tools must declare at least one effect_type"
                )
        if self.execution_class is ExecutionClass.PURE:
            if not self.deterministic:
                errors.append("PURE tools must be deterministic")
        if self.execution_class is ExecutionClass.EXTERNAL_IRREVERSIBLE:
            if self.reversible:
                errors.append(
                    "EXTERNAL_IRREVERSIBLE tools must declare reversible=False"
                )

        if errors:
            raise ToolManifestError(
                f"Invalid manifest '{self.name}': " + "; ".join(errors)
            )

    def declares_effect(self, effect_type: EffectType) -> bool:
        """Whether this manifest declares a given effect type."""
        return effect_type in self.effect_types


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of an OperationPolicy check against a manifest."""

    allowed: bool
    requires_approval: bool = False
    reason: str = ""


@dataclass(frozen=True)
class OperationPolicy:
    """Deployment-time control surface over tools. Deny-by-default."""

    allowed: frozenset[str] = frozenset()
    # ^ Tool names this deployment allows. A tool NOT listed is denied.
    requires_approval: frozenset[str] = frozenset()
    # ^ Tools whose invocation needs human approval before execution.
    max_wall_time_ms: int | None = None
    max_output_bytes: int | None = None
    network_access: bool = False
    filesystem_scope: str | None = None
    # ^ "private" | "shared-kb" | "workspace" | "system" | None (unrestricted)
    retry_policy: RetryPolicy | None = None
    reversible: bool = True
    # ^ False = irreversible tools are denied.

    def __post_init__(self) -> None:
        if self.filesystem_scope is not None and \
                self.filesystem_scope not in FILESYSTEM_SCOPES:
            raise ToolManifestError(
                f"invalid policy filesystem_scope: {self.filesystem_scope!r}"
            )
        if self.max_wall_time_ms is not None and self.max_wall_time_ms < 0:
            raise ToolManifestError("max_wall_time_ms must be >= 0")
        if self.max_output_bytes is not None and self.max_output_bytes < 0:
            raise ToolManifestError("max_output_bytes must be >= 0")
        if not self.requires_approval.issubset(self.allowed):
            raise ToolManifestError(
                "requires_approval must be a subset of allowed"
            )

    def decide(self, manifest: ToolManifest) -> PolicyDecision:
        """Check a tool manifest against this policy (deny-by-default).

        Returns allowed / denied / requires_approval with a reason.
        """
        if manifest.name not in self.allowed:
            return PolicyDecision(
                False, False,
                f"Tool '{manifest.name}' not in policy allowlist",
            )
        if manifest.name in self.requires_approval:
            return PolicyDecision(
                True, True, "Requires human approval per policy",
            )
        if not self.network_access and manifest.requires_network:
            return PolicyDecision(
                False, False,
                f"Tool '{manifest.name}' requires network; policy denies network",
            )
        if self.filesystem_scope and manifest.filesystem_scopes:
            scopes = set(manifest.filesystem_scopes)
            if "system" in scopes:
                return PolicyDecision(
                    False, False,
                    f"Tool '{manifest.name}' touches host filesystem "
                    f"(scope 'system'); policy allows '{self.filesystem_scope}'",
                )
            if scopes != {"none"} and self.filesystem_scope not in scopes:
                return PolicyDecision(
                    False, False,
                    f"Tool '{manifest.name}' scopes {sorted(scopes)} outside "
                    f"policy filesystem_scope '{self.filesystem_scope}'",
                )
        if (
            self.max_wall_time_ms is not None
            and manifest.max_runtime_ms is not None
            and manifest.max_runtime_ms > self.max_wall_time_ms
        ):
            return PolicyDecision(
                False, False,
                f"Tool '{manifest.name}' max_runtime_ms "
                f"{manifest.max_runtime_ms} exceeds policy cap "
                f"{self.max_wall_time_ms}",
            )
        if (
            self.max_output_bytes is not None
            and manifest.max_output_bytes is not None
            and manifest.max_output_bytes > self.max_output_bytes
        ):
            return PolicyDecision(
                False, False,
                f"Tool '{manifest.name}' max_output_bytes "
                f"{manifest.max_output_bytes} exceeds policy cap "
                f"{self.max_output_bytes}",
            )
        if not self.reversible and not manifest.reversible:
            return PolicyDecision(
                False, False,
                f"Tool '{manifest.name}' is irreversible; policy denies "
                "irreversible tools",
            )
        return PolicyDecision(True, False, "")


def builtin_manifests() -> dict[str, ToolManifest]:
    """Manifests for the v0.6.0 builtin tools (registered at startup).

    Execution-class mapping:
      read/ls          → READ_ONLY (frozen snapshot view)
      write/kb_write   → STAGED_MUTATION (staged, committed atomically)
      send_email       → STAGED_MUTATION (outbox-staged; discarded on
                         rollback before delivery)
      delegate         → STAGED_MUTATION (TASK_CREATE + EMAIL_SEND)
    """
    read = ToolManifest(
        name="read",
        version="1.0.0",
        execution_class=ExecutionClass.READ_ONLY,
        input_schema={"path": {"type": "string"}},
        output_schema={"content": {"type": "string"}},
        capabilities=("filesystem:read",),
        filesystem_scopes=("private",),
        deterministic=True,
        idempotent=True,
        max_output_bytes=1_000_000,
    )
    ls = ToolManifest(
        name="ls",
        version="1.0.0",
        execution_class=ExecutionClass.READ_ONLY,
        input_schema={"path": {"type": "string"}},
        output_schema={"entries": {"type": "array"}},
        capabilities=("filesystem:list",),
        filesystem_scopes=("private",),
        deterministic=True,
        idempotent=True,
        max_output_bytes=1_000_000,
    )
    write = ToolManifest(
        name="write",
        version="1.0.0",
        execution_class=ExecutionClass.STAGED_MUTATION,
        input_schema={
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        output_schema={"staged": {"type": "boolean"}},
        capabilities=("filesystem:write",),
        effect_types=(EffectType.FILE_WRITE,),
        filesystem_scopes=("private",),
        deterministic=True,
        idempotent=True,
        reversible=True,
    )
    kb_write = ToolManifest(
        name="kb_write",
        version="1.0.0",
        execution_class=ExecutionClass.STAGED_MUTATION,
        input_schema={
            "path": {"type": "string"},
            "content": {"type": "string"},
            "expected_version": {"type": "integer"},
        },
        output_schema={"staged": {"type": "boolean"}},
        capabilities=("kb:write",),
        effect_types=(EffectType.KB_WRITE,),
        filesystem_scopes=("shared-kb",),
        deterministic=True,
        idempotent=False,  # versioned — a stale write fails, not deduped
        reversible=True,
    )
    send_email = ToolManifest(
        name="send_email",
        version="1.0.0",
        execution_class=ExecutionClass.STAGED_MUTATION,
        input_schema={
            "to": {"type": "array"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        output_schema={"staged": {"type": "boolean"}},
        capabilities=("email:send",),
        effect_types=(EffectType.EMAIL_SEND,),
        filesystem_scopes=("none",),
        deterministic=True,
        idempotent=True,  # outbox idempotency keys dedupe
        reversible=True,  # discarded on rollback before delivery
        max_output_bytes=100_000,
    )
    delegate = ToolManifest(
        name="delegate",
        version="1.0.0",
        execution_class=ExecutionClass.STAGED_MUTATION,
        input_schema={
            "recipient_agent_id": {"type": "string"},
            "task_title": {"type": "string"},
            "task_description": {"type": "string"},
        },
        output_schema={"task_id": {"type": "string"}, "staged": {"type": "boolean"}},
        capabilities=("task:delegate",),
        effect_types=(EffectType.TASK_CREATE, EffectType.EMAIL_SEND),
        filesystem_scopes=("none",),
        deterministic=True,
        idempotent=False,
        reversible=True,
    )
    # v0.7.0 restricted tools (P1-3) — the safe set that precedes Bash
    # (see KANBAN/OPEN_ISSUE/OI-001.md).
    apply_patch = ToolManifest(
        name="apply_patch",
        version="1.0.0",
        execution_class=ExecutionClass.STAGED_MUTATION,
        input_schema={
            "path": {"type": "string"},
            "patch": {"type": "string"},
        },
        output_schema={"staged": {"type": "boolean"}},
        capabilities=("filesystem:patch",),
        effect_types=(EffectType.FILE_PATCH,),
        filesystem_scopes=("private",),
        deterministic=True,
        idempotent=False,   # context-dependent — conflict → reject
        reversible=True,    # rolled back via file_previous like FILE_WRITE
        max_output_bytes=1_000_000,
    )
    run_tests = ToolManifest(
        name="run_tests",
        version="1.0.0",
        execution_class=ExecutionClass.SANDBOXED_PROCESS,
        input_schema={"test_path": {"type": "string"}},
        output_schema={
            "success": {"type": "boolean"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "exit_code": {"type": "integer"},
        },
        capabilities=("test:run",),
        effect_types=(),
        filesystem_scopes=("workspace",),
        deterministic=False,    # depends on test results
        idempotent=True,
        reversible=True,
        requires_network=False,  # uv uses the synced environment
        max_runtime_ms=60_000,
        max_output_bytes=200_000,
    )
    git_diff = ToolManifest(
        name="git_diff",
        version="1.0.0",
        execution_class=ExecutionClass.READ_ONLY,
        input_schema={"path": {"type": "string"}},
        output_schema={"stdout": {"type": "string"}},
        capabilities=("git:diff",),
        effect_types=(),
        filesystem_scopes=("workspace",),
        deterministic=False,    # output depends on repo state
        idempotent=True,
        max_runtime_ms=10_000,
        max_output_bytes=200_000,
    )
    git_status = ToolManifest(
        name="git_status",
        version="1.0.0",
        execution_class=ExecutionClass.READ_ONLY,
        input_schema={},
        output_schema={"stdout": {"type": "string"}},
        capabilities=("git:status",),
        effect_types=(),
        filesystem_scopes=("workspace",),
        deterministic=False,
        idempotent=True,
        max_runtime_ms=10_000,
        max_output_bytes=200_000,
    )
    return {
        m.name: m
        for m in (
            read, ls, write, kb_write, send_email, delegate,
            apply_patch, run_tests, git_diff, git_status,
        )
    }
