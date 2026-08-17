"""Executor registration + admission (v0.8.0 P1-4/5).

The kernel never executes an async tool op by itself: a registered
executor must accept it first. Executor Admission sits between
submission (Act) and execution (Publish dispatch):

  SUBMITTED → Admission (executor registered? tier compatible?
              capacity available?) → PENDING (claimed by executor)
                                     → result → Ingest

Executors are registered per tool with a trusted_level tier:

  TRUSTED_IN_PROCESS         — builtin kernel-side executors (host
                               subprocess tools like run_tests)
  UNTRUSTED_OUT_OF_PROCESS   — third-party / remote executors
  SANDBOXED_OUT_OF_PROCESS   — real OS-level isolation (no builtin
                               tool qualifies yet — run_tests real
                               isolation is v0.8 P2-7)

Tier compatibility is derived from the tool manifest's
execution_class: only tools whose class requires an executor go
through dispatch; PURE / READ_ONLY / STAGED_MUTATION tools are
kernel-executed at Act and never need one.

Capacity is count-based and stateless: the admission check counts
in-flight ops for the tool against max_concurrent. A tool at capacity
stays SUBMITTED and is re-admitted on a later tick (backpressure).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from my_team.tool_manifest import ExecutionClass, ToolManifest


class ExecutorTier(str, Enum):
    """Trusted level of a registered tool executor."""

    TRUSTED_IN_PROCESS = "trusted_in_process"
    UNTRUSTED_OUT_OF_PROCESS = "untrusted_out_of_process"
    SANDBOXED_OUT_OF_PROCESS = "sandboxed_out_of_process"


# Execution classes that never need an executor: the kernel executes
# them at Act (reads / staged mutations are kernel-side operations).
KERNEL_EXECUTED_CLASSES = frozenset({
    ExecutionClass.PURE,
    ExecutionClass.READ_ONLY,
    ExecutionClass.LOCAL_DETERMINISTIC,
    ExecutionClass.STAGED_MUTATION,
})

# Execution class → tiers that may execute it.
_EXECUTOR_TIERS_BY_CLASS: dict[ExecutionClass, tuple[ExecutorTier, ...]] = {
    ExecutionClass.LOCAL_PROCESS: (
        ExecutorTier.TRUSTED_IN_PROCESS,
        ExecutorTier.UNTRUSTED_OUT_OF_PROCESS,
    ),
    ExecutionClass.SANDBOXED_PROCESS: (
        ExecutorTier.SANDBOXED_OUT_OF_PROCESS,
    ),
    ExecutionClass.EXTERNAL_IRREVERSIBLE: (
        ExecutorTier.UNTRUSTED_OUT_OF_PROCESS,
    ),
}


def requires_executor(execution_class: ExecutionClass | None) -> bool:
    """Whether tools of this class go through executor dispatch."""
    if execution_class is None:
        # No manifest — legacy path: keep the op in flight (no
        # admission information), the harness completes it.
        return True
    return execution_class not in KERNEL_EXECUTED_CLASSES


def required_tiers(
    execution_class: ExecutionClass | None,
) -> tuple[ExecutorTier, ...] | None:
    """Tiers that may execute this class; None = kernel-executed."""
    if execution_class is None:
        return (ExecutorTier.UNTRUSTED_OUT_OF_PROCESS,)
    if execution_class in KERNEL_EXECUTED_CLASSES:
        return None
    return _EXECUTOR_TIERS_BY_CLASS.get(execution_class)


@dataclass(frozen=True)
class ExecutorRecord:
    """A registered executor for one tool."""

    executor_id: str
    tool_name: str
    tier: ExecutorTier
    max_concurrent: int = 4


class ExecutorRegistry:
    """Per-tool executor registration with admission checks."""

    def __init__(self) -> None:
        self._executors: dict[str, ExecutorRecord] = {}

    def register(
        self,
        tool_name: str,
        *,
        tier: ExecutorTier,
        executor_id: str = "",
        max_concurrent: int = 4,
    ) -> ExecutorRecord:
        """Register an executor for a tool (replaces any prior one)."""
        record = ExecutorRecord(
            executor_id=executor_id or f"exe.{tool_name}.{tier.value}",
            tool_name=tool_name,
            tier=tier,
            max_concurrent=max_concurrent,
        )
        self._executors[tool_name] = record
        return record

    def unregister(self, tool_name: str) -> None:
        """Remove the executor for a tool (admission then fails)."""
        self._executors.pop(tool_name, None)

    def get(self, tool_name: str) -> ExecutorRecord | None:
        return self._executors.get(tool_name)

    def admit(
        self,
        tool_name: str,
        manifest: ToolManifest | None,
        in_flight: int,
    ) -> tuple[bool, str, bool]:
        """Admission check.

        Returns (admitted, reason, retryable):
        - admitted=True: the op may be dispatched now
        - retryable=True: capacity pressure only — keep the op queued
          (SUBMITTED) and re-admit on a later tick
        - retryable=False: permanent (no executor / tier mismatch) —
          the op fails with a structured error

        Checks, in order: executor registered → tier compatible with
        the manifest's execution class → capacity available.
        """
        record = self._executors.get(tool_name)
        if record is None:
            return (
                False,
                f"No executor registered for tool '{tool_name}' — "
                "admission denied",
                False,
            )
        tiers = required_tiers(
            manifest.execution_class if manifest is not None else None,
        )
        if tiers is not None and record.tier not in tiers:
            return (
                False,
                f"Executor tier '{record.tier.value}' cannot execute "
                f"'{tool_name}' (requires "
                f"{'/'.join(t.value for t in tiers)})",
                False,
            )
        if in_flight >= record.max_concurrent:
            return (
                False,
                f"Executor '{record.executor_id}' at capacity "
                f"({in_flight}/{record.max_concurrent})",
                True,
            )
        return True, "", False

    def tier(self, tool_name: str) -> ExecutorTier | None:
        record = self._executors.get(tool_name)
        return record.tier if record is not None else None

    def summary(self) -> dict[str, Any]:
        return {
            tool: {
                "executor_id": r.executor_id,
                "tier": r.tier.value,
                "max_concurrent": r.max_concurrent,
            }
            for tool, r in sorted(self._executors.items())
        }
