"""Agent-side tool handlers: private-workspace file tools + executor/workspace tools.

Per N1C_DEVICE_REFIT_DESIGN.md §2:

- **私密区文件（非设备，§4.5）**：read / ls / write / apply_patch
  These tools operate on an agent's private workspace (PrivateStore).
  They are NOT device-bound; device_id="", capability is the manifest-
  derived uuid5 (adopt mechanism — unchanged from before).
  Authorization path is unchanged (Authority two-layer Grant).

- **执行器/工作区（非设备，§3.4）**：run_tests / python_compute /
  python_transform / git_diff / git_status
  These tools belong to the kernel execution surface (executor + sandbox).
  Only moved here from simulation._register_tool_handlers; semantics,
  ToolResult fields, and executor registration are unchanged.

Each public function returns a handler callable with the standard signature
``(context: ToolContext, **kwargs) -> ToolResult`` ready for
``simulation.register_tool(manifest, handler)``.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from posixpath import normpath
from typing import TYPE_CHECKING, Any, Callable

from my_team.agent_runtime import ToolContext, ToolResult
from my_team.audit import AuditEventType, AuditLog
from my_team.patch_ops import PatchError, apply_patch
from my_team.private_store import AccessDeniedError, PrivateStore
from my_team.python_worker import DEFAULT_ALLOWED_MODULES, run_python_compute, run_python_transform
from my_team.sandbox_tools import make_workspace_copy, run_sandboxed_process
from my_team.transaction import EffectType, TransactionBuffer

if TYPE_CHECKING:
    from my_team.agent_runtime import ToolRegistry


# ---------------------------------------------------------------------------
# Helper types
# ---------------------------------------------------------------------------

# Callable for "read agent's committed+staged private file content"
# Returns (found: bool, content: str)
_interrupt_agentPrivateFile = Callable[[str, str], tuple[bool, str]]
# Callable for "get staged private path→content overlay for an agent"
_StagedPrivateEffects = Callable[[str], dict[str, str]]


def _validate_write_path(path: str) -> str | None:
    """Return error message if path is invalid for a private-workspace write."""
    if not path:
        return "write path must not be empty"
    if os.path.isabs(path):
        return f"absolute paths not allowed: {path}"
    parts = normpath(path).split("/")
    if ".." in parts:
        return f"path traversal rejected: {path}"
    return None


# ---------------------------------------------------------------------------
# §4.5 Private-workspace file tools
# ---------------------------------------------------------------------------


def make_handle_read(
    private_store: PrivateStore,
    staged_private_effects: _StagedPrivateEffects,
    read_private_file: _interrupt_agentPrivateFile,
) -> Callable[..., Any]:
    """Return the ``read`` tool handler."""

    def handle_read(context: ToolContext, path: str = "", **_kw: Any) -> Any:
        try:
            found, content = read_private_file(context.agent_id, path)
        except AccessDeniedError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                agent_id=context.agent_id,
                tool_name="read",
                tick=context.tick,
            )
        if not found:
            return ToolResult(
                success=False,
                error=f"File not found: {path}",
                agent_id=context.agent_id,
                tool_name="read",
                tick=context.tick,
            )
        return ToolResult(
            success=True,
            data={"content": content},
            agent_id=context.agent_id,
            tool_name="read",
            tick=context.tick,
        )

    return handle_read


def make_handle_ls(
    private_store: PrivateStore,
    staged_private_effects: _StagedPrivateEffects,
) -> Callable[..., Any]:
    """Return the ``ls`` tool handler."""

    def handle_ls(context: ToolContext, path: str = "", **_kw: Any) -> Any:
        try:
            target = (
                private_store.resolve_path(
                    context.agent_id,
                    path,
                )
                if path
                else private_store.agent_home(context.agent_id)
            )
            home = private_store.agent_home(context.agent_id)
        except AccessDeniedError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                agent_id=context.agent_id,
                tool_name="ls",
                tick=context.tick,
            )
        if not target.exists():
            return ToolResult(
                success=False,
                error=f"Directory not found: {path}",
                agent_id=context.agent_id,
                tool_name="ls",
                tick=context.tick,
            )
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries: set[str] = set()
        for p in home.rglob("*"):
            rel = p.relative_to(home).as_posix()
            if rel.startswith(prefix):
                rest = rel[len(prefix) :]
                if rest and "/" not in rest:
                    entries.add(rest)
        for rel in staged_private_effects(context.agent_id):
            if rel.startswith(prefix):
                rest = rel[len(prefix) :]
                if rest and "/" not in rest:
                    entries.add(rest)
        return ToolResult(
            success=True,
            data={"entries": sorted(entries)},
            agent_id=context.agent_id,
            tool_name="ls",
            tick=context.tick,
        )

    return handle_ls


def make_handle_write(
    transaction_buffer: TransactionBuffer,
) -> Callable[..., Any]:
    """Return the ``write`` tool handler."""

    def handle_write(
        context: ToolContext,
        path: str = "",
        content: str = "",
        **_kw: Any,
    ) -> Any:
        err = _validate_write_path(path)
        if err is not None:
            return ToolResult(
                success=False,
                error=err,
                error_code="INVALID_ARGUMENT",
                agent_id=context.agent_id,
                tool_name="write",
                tick=context.tick,
            )
        transaction_buffer.stage(
            effect_type=EffectType.FILE_WRITE,
            agent_id=context.agent_id,
            resource=path,
            data={"content": content},
        )
        return ToolResult(
            success=True,
            data={"staged": True},
            agent_id=context.agent_id,
            tool_name="write",
            tick=context.tick,
        )

    return handle_write


def make_handle_apply_patch(
    transaction_buffer: TransactionBuffer,
    read_private_file: _interrupt_agentPrivateFile,
) -> Callable[..., Any]:
    """Return the ``apply_patch`` tool handler."""

    def handle_apply_patch(
        context: ToolContext,
        path: str = "",
        patch: str = "",
        **_kw: Any,
    ) -> Any:
        if not path:
            return ToolResult(
                success=False,
                error="apply_patch requires 'path'",
                error_code="invalid_patch",
                retryable=False,
                agent_id=context.agent_id,
                tool_name="apply_patch",
                tick=context.tick,
            )
        err = _validate_write_path(path)
        if err is not None:
            return ToolResult(
                success=False,
                error=err,
                error_code="INVALID_ARGUMENT",
                retryable=False,
                agent_id=context.agent_id,
                tool_name="apply_patch",
                tick=context.tick,
            )
        if not patch:
            return ToolResult(
                success=False,
                error="apply_patch requires 'patch'",
                error_code="invalid_patch",
                retryable=False,
                agent_id=context.agent_id,
                tool_name="apply_patch",
                tick=context.tick,
            )
        found, base_content = read_private_file(context.agent_id, path)
        content = base_content if found else ""
        try:
            new_content = apply_patch(content, patch)
        except PatchError as e:
            return ToolResult(
                success=False,
                error=f"patch rejected: {e}",
                error_code="patch_conflict" if e.conflict else "invalid_patch",
                retryable=False,
                agent_id=context.agent_id,
                tool_name="apply_patch",
                tick=context.tick,
            )

        def _sha(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        transaction_buffer.stage(
            effect_type=EffectType.FILE_PATCH,
            agent_id=context.agent_id,
            resource=path,
            data={
                "content": new_content,
                "patch": patch,
                "base_hash": _sha(content),
                "patch_hash": _sha(patch),
                "new_content_hash": _sha(new_content),
            },
        )
        return ToolResult(
            success=True,
            data={
                "staged": True,
                "base_hash": _sha(content),
                "new_content_hash": _sha(new_content),
            },
            agent_id=context.agent_id,
            tool_name="apply_patch",
            tick=context.tick,
        )

    return handle_apply_patch


# ---------------------------------------------------------------------------
# §3.4 Executor/workspace tools
# ---------------------------------------------------------------------------


def make_handle_run_tests(
    tool_registry: ToolRegistry,
    audit_log: AuditLog,
    active_processes: dict[str, Any],
) -> Callable[..., Any]:
    """Return the ``run_tests`` tool handler."""

    def handle_run_tests(context: ToolContext, test_path: str = "", **_kw: Any) -> Any:
        manifest = tool_registry.get_manifest("run_tests")
        assert manifest is not None
        timeout_ms = manifest.max_runtime_ms or 60_000
        max_output = manifest.max_output_bytes or 200_000
        constraints = manifest.sandbox_constraints
        with make_workspace_copy() as copy:
            target = test_path
            if target and not os.path.isabs(target):
                target = str(copy / target)
            cmd = [sys.executable, "-I", "-m", "pytest", "-q"]
            if target:
                cmd.append(target)
            res = run_sandboxed_process(
                cmd,
                timeout_ms=timeout_ms,
                max_output_bytes=max_output,
                cwd=str(copy),
                constraints=constraints,
                home=str(copy),
                on_start=(
                    lambda proc: (
                        active_processes.__setitem__(
                            context.request_id,
                            proc,
                        )
                        if context.request_id
                        else None
                    )
                ),
                on_end=(
                    lambda proc: (
                        active_processes.pop(
                            context.request_id,
                            None,
                        )
                        if context.request_id
                        else None
                    )
                ),
            )
        if res["timed_out"]:
            audit_log.record(
                AuditEventType.TOOL_TIMEOUT,
                agent_id=context.agent_id,
                tick=context.tick,
                details={"tool": "run_tests", "timeout_ms": timeout_ms},
                success=False,
                error=f"run_tests timed out after {timeout_ms}ms",
            )
        return ToolResult(
            success=res["success"],
            data=res,
            error=(
                None
                if res["success"]
                else f"tests failed (exit {res['exit_code']})"
                + (" [timed out]" if res["timed_out"] else "")
            ),
            error_code="tool_timeout" if res["timed_out"] else None,
            retryable=not res["timed_out"],
            agent_id=context.agent_id,
            tool_name="run_tests",
            tick=context.tick,
        )

    return handle_run_tests


def make_handle_python_compute(
    tool_registry: ToolRegistry,
    active_processes: dict[str, Any],
) -> Callable[..., Any]:
    """Return the ``python_compute`` tool handler."""

    def handle_python_compute(
        context: ToolContext,
        code: str = "",
        inputs: dict[str, Any] | None = None,
        allowed_modules: list[str] | None = None,
        **_kw: Any,
    ) -> Any:
        manifest = tool_registry.get_manifest("python_compute")
        assert manifest is not None
        res = run_python_compute(
            code=code,
            inputs=dict(inputs or {}),
            allowed_modules=tuple(allowed_modules or DEFAULT_ALLOWED_MODULES),
            timeout_ms=manifest.max_runtime_ms or 10_000,
            max_output_bytes=manifest.max_output_bytes or 200_000,
            on_start=(
                lambda proc: (
                    active_processes.__setitem__(
                        context.request_id,
                        proc,
                    )
                    if context.request_id
                    else None
                )
            ),
            on_end=(
                lambda proc: (
                    active_processes.pop(
                        context.request_id,
                        None,
                    )
                    if context.request_id
                    else None
                )
            ),
        )
        return ToolResult(
            success=res["success"],
            data=res,
            error=(None if res["success"] else res.get("error", "python_compute failed")),
            error_code="tool_timeout" if res["timed_out"] else None,
            retryable=not res["timed_out"],
            agent_id=context.agent_id,
            tool_name="python_compute",
            tick=context.tick,
        )

    return handle_python_compute


def make_handle_python_transform(
    tool_registry: ToolRegistry,
    active_processes: dict[str, Any],
    read_private_file: _interrupt_agentPrivateFile,
) -> Callable[..., Any]:
    """Return the ``python_transform`` tool handler."""

    def handle_python_transform(
        context: ToolContext,
        code: str = "",
        inputs: dict[str, Any] | None = None,
        input_files: dict[str, str] | None = None,
        allowed_modules: list[str] | None = None,
        **_kw: Any,
    ) -> Any:
        manifest = tool_registry.get_manifest("python_transform")
        assert manifest is not None
        resolved: dict[str, str] = {}
        missing: list[str] = []
        for rel in input_files or {}:
            try:
                found, content = read_private_file(context.agent_id, rel)
            except AccessDeniedError:
                missing.append(rel)
                continue
            if found:
                resolved[rel] = content
            else:
                missing.append(rel)
        if missing:
            return ToolResult(
                success=False,
                data={},
                error=("input_files not in frozen workspace view: " + ", ".join(sorted(missing))),
                error_code="invalid_argument",
                agent_id=context.agent_id,
                tool_name="python_transform",
                tick=context.tick,
            )
        res = run_python_transform(
            code=code,
            inputs=dict(inputs or {}),
            input_files=resolved,
            allowed_modules=tuple(allowed_modules or DEFAULT_ALLOWED_MODULES),
            timeout_ms=manifest.max_runtime_ms or 30_000,
            max_output_bytes=manifest.max_output_bytes or 200_000,
            on_start=(
                lambda proc: (
                    active_processes.__setitem__(
                        context.request_id,
                        proc,
                    )
                    if context.request_id
                    else None
                )
            ),
            on_end=(
                lambda proc: (
                    active_processes.pop(
                        context.request_id,
                        None,
                    )
                    if context.request_id
                    else None
                )
            ),
        )
        return ToolResult(
            success=res["success"],
            data=res,
            error=(None if res["success"] else res.get("error", "python_transform failed")),
            error_code="tool_timeout" if res["timed_out"] else None,
            retryable=not res["timed_out"],
            agent_id=context.agent_id,
            tool_name="python_transform",
            tick=context.tick,
        )

    return handle_python_transform


def make_handle_git_diff(
    tool_registry: ToolRegistry,
    audit_log: AuditLog,
) -> Callable[..., Any]:
    """Return the ``git_diff`` tool handler."""

    def handle_git_diff(context: ToolContext, path: str = "", **_kw: Any) -> Any:
        manifest = tool_registry.get_manifest("git_diff")
        assert manifest is not None
        cmd = ["git", "diff", "--"]
        if path:
            cmd = ["git", "diff", "--", path]
        res = run_sandboxed_process(
            cmd,
            timeout_ms=manifest.max_runtime_ms or 10_000,
            max_output_bytes=manifest.max_output_bytes or 200_000,
            cwd=str(Path.cwd()),
        )
        if res["timed_out"]:
            audit_log.record(
                AuditEventType.TOOL_TIMEOUT,
                agent_id=context.agent_id,
                tick=context.tick,
                details={"tool": "git_diff"},
                success=False,
                error="git_diff timed out",
            )
        return ToolResult(
            success=res["success"],
            data=res,
            error=None if res["success"] else res["stderr"],
            error_code="tool_timeout" if res["timed_out"] else None,
            agent_id=context.agent_id,
            tool_name="git_diff",
            tick=context.tick,
        )

    return handle_git_diff


def make_handle_git_status(
    tool_registry: ToolRegistry,
    audit_log: AuditLog,
) -> Callable[..., Any]:
    """Return the ``git_status`` tool handler."""

    def handle_git_status(context: ToolContext, **_kw: Any) -> Any:
        manifest = tool_registry.get_manifest("git_status")
        assert manifest is not None
        res = run_sandboxed_process(
            ["git", "status", "--short"],
            timeout_ms=manifest.max_runtime_ms or 10_000,
            max_output_bytes=manifest.max_output_bytes or 200_000,
            cwd=str(Path.cwd()),
        )
        if res["timed_out"]:
            audit_log.record(
                AuditEventType.TOOL_TIMEOUT,
                agent_id=context.agent_id,
                tick=context.tick,
                details={"tool": "git_status"},
                success=False,
                error="git_status timed out",
            )
        return ToolResult(
            success=res["success"],
            data=res,
            error=None if res["success"] else res["stderr"],
            error_code="tool_timeout" if res["timed_out"] else None,
            agent_id=context.agent_id,
            tool_name="git_status",
            tick=context.tick,
        )

    return handle_git_status
