"""ToolRequest / ToolResult contract (v0.8.0 P1-3).

The wire contract between the kernel and tool executors:

- ToolRequest is built by the KERNEL at Act time. Every identity and
  context field (agent_id, task_id, state_epoch, workspace_version,
  manifest_hash, input_hash) is system-injected — an executor or a
  plugin must never supply these itself (plugin security model).
- ToolResultContract is produced by the executor and accepted by
  registry.complete_tool(). Correlation is by request_id; the
  contract fields (output_hash, declared/observed/possible effects,
  executor_cancel_confirmed) are recorded on the op for audit.

Distinction from agent_runtime.ToolResult: that type is the
in-kernel result of a synchronous registry.execute() call. This
contract is for ASYNC tool ops that span ticks through the pending
operation registry.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization (sorted keys, compact)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def hash_payload(obj: Any) -> str:
    """sha256 of the canonical JSON of a payload."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


class ToolRequest(BaseModel):
    """System-built request handed to a tool executor (async path)."""

    request_id: str = Field(description="Pending-operation correlation id")
    agent_id: str = Field(description="Initiating agent (system-injected)")
    task_id: str = Field(default="", description="Associated task")
    tool_name: str = Field(description="Tool being invoked")
    tool_version: str = Field(description="Manifest version at submission")
    manifest_hash: str = Field(description="sha256 of the ToolManifest contract")
    input_hash: str = Field(description="sha256 of canonical arguments")
    state_epoch: int = Field(
        description="State epoch the request was based on — results for "
                    "a superseded epoch are fenced by Ingest",
    )
    workspace_version: str = Field(
        default="0",
        description="Hash of the agent's frozen workspace view this "
                    "request was based on (informational; apply-time "
                    "FILE_PATCH base-hash checks are the enforcement)",
    )
    created_tick: int = Field(description="Tick the request was submitted")
    deadline_tick: int | None = Field(
        default=None, description="Tick after which the op times out",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Tool arguments (payload)",
    )


class ToolResultContract(BaseModel):
    """Structured result produced by a tool executor (async path)."""

    request_id: str = Field(description="Correlates to the ToolRequest")
    status: str = Field(
        default="completed",
        description="completed / failed / cancelled / timed_out "
                    "(informational — the op lifecycle is authoritative)",
    )
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    data: dict[str, Any] = Field(
        default_factory=dict, description="Structured tool output",
    )
    output_hash: str = Field(
        default="", description="sha256 of canonical data (executor-computed)",
    )
    effects: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Effect disclosure split by trust: "
                    '{"declared": [...], "observed": [...], "possible": [...]}',
    )
    executor_cancel_confirmed: bool = Field(
        default=False,
        description="True only when the executor confirmed physical "
                    "termination of the work",
    )
    state_epoch: int = Field(
        default=0, description="Echoed from the ToolRequest (fencing)",
    )
