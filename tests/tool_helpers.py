"""Shared test helpers for tool registration.

v0.7.0: every tool used through SubmitToolRequest must have a
registered manifest (PreValidate Check 1d). Mock remote tools
(executed by FakeToolExecutor outside the registry) declare
EXTERNAL_IRREVERSIBLE manifests; the registry handler is a no-op
because the executor, not the registry, produces the result.
"""

from __future__ import annotations

from typing import Any

from my_team.agent_runtime import ToolRegistry
from my_team.tool_manifest import ExecutionClass, ToolManifest


def register_remote_tool(
    registry: ToolRegistry,
    name: str,
    *,
    input_schema: dict[str, Any] | None = None,
    requires_network: bool = True,
) -> None:
    """Register a manifest for a mock remote tool (external executor).

    The handler is a no-op: remote tools never execute through the
    registry — FakeToolExecutor completes them out-of-band.
    """
    registry.register_handler(
        name,
        lambda **_: None,
        manifest=ToolManifest(
            name=name,
            version="1.0.0",
            execution_class=ExecutionClass.EXTERNAL_IRREVERSIBLE,
            input_schema=input_schema or {},
            requires_network=requires_network,
            reversible=False,
        ),
    )
