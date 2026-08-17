"""Shared test helpers for tool registration.

v0.7.0: every tool used through SubmitToolRequest must have a
registered manifest (PreValidate Check 1d). Mock remote tools
(executed by FakeToolExecutor outside the registry) declare
EXTERNAL_IRREVERSIBLE manifests; the registry handler is a no-op
because the executor, not the registry, produces the result.

v0.8.0 (P1-4/5): remote tools additionally need a registered executor
for Executor Admission. register_remote_tool accepts the Simulation
(or a bare ToolRegistry) and registers an UNTRUSTED_OUT_OF_PROCESS
executor for the tool when a simulation is given — dispatch then
claims the op as PENDING for the harness to complete out-of-band.
"""

from __future__ import annotations

from typing import Any

from my_team.executor_registry import ExecutorTier
from my_team.tool_manifest import ExecutionClass, ToolManifest


def register_remote_tool(
    registry_or_sim: Any,
    name: str,
    *,
    input_schema: dict[str, Any] | None = None,
    requires_network: bool = True,
    supports_cancel: bool = True,
    max_concurrent: int = 4,
) -> None:
    """Register a manifest for a mock remote tool (external executor).

    Accepts a Simulation (registers the tool + an UNTRUSTED_OUT_OF_
    PROCESS executor) or a bare ToolRegistry (manifest only).

    The handler is a no-op: remote tools never execute through the
    registry — FakeToolExecutor completes them out-of-band.
    """
    sim = registry_or_sim if hasattr(registry_or_sim, "_tool_registry") else None
    registry = (
        registry_or_sim._tool_registry if sim is not None else registry_or_sim
    )
    registry.register_handler(
        name,
        lambda **_: None,
        manifest=ToolManifest(
            name=name,
            version="1.0.0",
            execution_class=ExecutionClass.EXTERNAL_IRREVERSIBLE,
            input_schema=input_schema or {},
            requires_network=requires_network,
            supports_cancel=supports_cancel,
            reversible=False,
        ),
    )
    if sim is not None:
        sim._executors.register(
            name,
            tier=ExecutorTier.UNTRUSTED_OUT_OF_PROCESS,
            max_concurrent=max_concurrent,
        )
