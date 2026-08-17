"""Pending Operation Registry — tracks all in-flight external operations.

Per the architectural redesign: LLM requests, tool invocations, and human
decisions are external operations that span multiple ticks. The registry
tracks their lifecycle:

  SUBMITTED → PENDING → COMPLETED / FAILED / CANCELLED / TIMED_OUT

Each operation has:
  - request_id: unique identifier for correlation
  - agent_id: which agent initiated it
  - task_id: which task it belongs to
  - activation_id: which activation produced it
  - created_tick: when it was submitted
  - eligible_tick: when the result becomes visible to the simulation
  - deadline_tick: when it times out
  - status: current lifecycle state
  - result: the result data (set on completion)
  - error: error message (set on failure)
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from my_team.tool_protocol import ToolRequest, ToolResultContract


class OpStatus(str, Enum):
    """Lifecycle status of a pending operation."""

    SUBMITTED = "submitted"    # Intent committed, not yet dispatched
    PENDING = "pending"        # Dispatched to external system
    COMPLETED = "completed"    # External system returned result
    FAILED = "failed"          # External system returned error
    CANCELLED = "cancelled"    # Cancelled by system or agent
    TIMED_OUT = "timed_out"    # Exceeded deadline_tick


class OpType(str, Enum):
    """Type of external operation."""

    LLM_REQUEST = "llm_request"
    TOOL_REQUEST = "tool_request"
    EMAIL_DELIVERY = "email_delivery"
    HUMAN_DECISION = "human_decision"
    LOCK_ACQUISITION = "lock_acquisition"


class CancellationResult(BaseModel):
    """Structured outcome of a cancel_operation() call (v0.7.0 review).

    Distinguishes the levels of a cancel:
    - accepted: the registry cancelled the op and fenced its result
    - executor_cancel_requested / executor_cancel_confirmed: whether an
      actual executor could be SIGNALED (no executor exists in-kernel
      for remote tools — the external harness is out of reach)
    - external_effects_possible: the op MAY already have external side
      effects (provider processing, cost, logs) that cancellation
      cannot undo — cancellation is logical, not physical
    """

    accepted: bool
    request_id: str
    op_type: OpType | None = None
    reason: str = ""
    result_fenced: bool = False
    executor_cancel_requested: bool = False
    executor_cancel_confirmed: bool = False
    external_effects_possible: bool = False


class PendingOperation(BaseModel):
    """A single in-flight external operation."""

    request_id: str = Field(
        default_factory=lambda: f"op.{uuid.uuid4().hex[:12]}",
        description="Unique operation identifier",
    )
    op_type: OpType = Field(description="Type of operation")
    agent_id: str = Field(description="Agent that initiated this operation")
    task_id: str = Field(default="", description="Associated task ID")
    activation_id: str = Field(default="", description="Activation that produced this")
    created_tick: int = Field(description="Tick when operation was submitted")
    eligible_tick: int = Field(
        default=0,
        description="Tick when result becomes visible (created_tick + latency)",
    )
    deadline_tick: int | None = Field(
        default=None,
        description="Tick when operation times out (None = no timeout)",
    )
    status: OpStatus = Field(default=OpStatus.SUBMITTED)
    result: dict[str, Any] = Field(
        default_factory=dict,
        description="Result data (set on completion)",
    )
    error: str = Field(default="", description="Error message (set on failure)")
    retry_count: int = Field(default=0, ge=0, description="Number of retries")
    max_retries: int = Field(default=3, ge=0, description="Maximum retries")
    state_epoch: int = Field(
        default=0,
        description="State epoch at submission — results from an older "
                    "epoch are stale and discarded by Ingest (fencing)",
    )
    tool_request: ToolRequest | None = Field(
        default=None,
        description="System-built tool contract for TOOL_REQUEST ops "
                    "(v0.8.0 P1-3); None for non-tool ops or legacy "
                    "submissions",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional operation metadata",
    )


class PendingOperationRegistry:
    """Central registry for all in-flight external operations.

    Operations flow through this lifecycle:

    1. Agent produces Intent (SubmitLLMRequest / SubmitToolRequest / etc.)
    2. Intent is committed as StagedEffect in Phase 7 (Commit)
    3. Registry creates PendingOperation with status SUBMITTED
    4. Phase 8 (Publish) dispatches to external systems, status → PENDING
    5. External system completes, status → COMPLETED
    6. Phase 1 (Ingest) collects completed operations
    7. Results are published as WakeEvents for agent re-activation
    """

    def __init__(self) -> None:
        self._operations: dict[str, PendingOperation] = {}
        # Agent-chosen request_id history (v0.8.0 P1-6): persisted
        # across restart so a replayed request_id is rejected — no
        # double charging / double side effects. Keyed by the agent's
        # request_id (op.metadata["request_id"]), scoped per agent.
        self._seen_requests: dict[str, dict[str, Any]] = {}

    def record_submitted(self, op: PendingOperation) -> None:
        """Remember the agent-chosen request_id of a submitted op."""
        req = op.metadata.get("request_id")
        if not req:
            return
        self._seen_requests[req] = {
            "status": op.status.value,
            "op_type": op.op_type.value,
            "tool_name": op.metadata.get("tool_name", ""),
            "agent_id": op.agent_id,
        }

    def is_seen(self, agent_id: str, request_id: str) -> bool:
        """Whether this agent ever submitted the request_id (any status)."""
        seen = self._seen_requests.get(request_id)
        return seen is not None and seen.get("agent_id") == agent_id

    def seen_requests_snapshot(self) -> dict[str, Any]:
        """Persisted history (request_id → submission record)."""
        return dict(self._seen_requests)

    def restore_seen_requests(self, snapshot: dict[str, Any]) -> None:
        self._seen_requests = dict(snapshot or {})

    def submit(
        self,
        op_type: OpType,
        agent_id: str,
        created_tick: int,
        eligible_tick: int = 0,
        deadline_tick: int | None = None,
        task_id: str = "",
        activation_id: str = "",
        metadata: dict[str, Any] | None = None,
        state_epoch: int = 0,
    ) -> PendingOperation:
        """Register a new pending operation.

        state_epoch: the simulation's state epoch at submission. If the
        epoch advances (rollback/restore) before the result arrives, the
        result is stale and Ingest discards it.
        """
        op = PendingOperation(
            op_type=op_type,
            agent_id=agent_id,
            created_tick=created_tick,
            eligible_tick=eligible_tick or created_tick + 1,
            deadline_tick=deadline_tick,
            task_id=task_id,
            activation_id=activation_id,
            metadata=metadata or {},
            state_epoch=state_epoch,
        )
        self._operations[op.request_id] = op
        self.record_submitted(op)
        return op

    def complete(
        self,
        request_id: str,
        result: dict[str, Any] | None = None,
    ) -> PendingOperation | None:
        """Mark an operation as completed.

        Terminal statuses (CANCELLED, TIMED_OUT, FAILED) are never
        overridden — a late result for a dead operation is ignored.
        """
        op = self._operations.get(request_id)
        if op is None:
            return None
        if op.status in {
            OpStatus.CANCELLED,
            OpStatus.TIMED_OUT,
            OpStatus.FAILED,
        }:
            return op
        op.status = OpStatus.COMPLETED
        op.result = result or {}
        return op

    def complete_tool(
        self,
        request_id: str,
        result: ToolResultContract,
    ) -> PendingOperation | None:
        """Complete a TOOL_REQUEST op with a structured contract result.

        Correlation: result.request_id must match the op. The contract
        fields (output_hash, effects disclosure, cancel confirmation)
        are recorded on the op for audit. Terminal ops (cancelled /
        timed out / failed) never accept a late result — the op's
        lifecycle stays authoritative over result.status.
        """
        op = self._operations.get(request_id)
        if op is None:
            return None
        if op.status in {
            OpStatus.CANCELLED,
            OpStatus.TIMED_OUT,
            OpStatus.FAILED,
        }:
            return op
        if result.request_id != request_id:
            return op
        op.status = OpStatus.COMPLETED
        op.result = result.data
        op.metadata["tool_result"] = result.model_dump(mode="json")
        return op

    def fail(
        self,
        request_id: str,
        error: str,
    ) -> PendingOperation | None:
        """Mark an operation as failed."""
        op = self._operations.get(request_id)
        if op is None:
            return None
        op.status = OpStatus.FAILED
        op.error = error
        return op

    def cancel(
        self,
        request_id: str,
    ) -> PendingOperation | None:
        """Cancel an in-flight operation (SUBMITTED/PENDING).

        Terminal or completed operations cannot be cancelled; a late
        result for a cancelled operation is ignored (complete() returns
        early for CANCELLED) — the result is never published.
        """
        op = self._operations.get(request_id)
        if op is None:
            return None
        if op.status not in {OpStatus.SUBMITTED, OpStatus.PENDING}:
            return None
        op.status = OpStatus.CANCELLED
        return op

    def timeout_expired(
        self,
        current_tick: int,
    ) -> list[PendingOperation]:
        """Find operations that have exceeded their deadline.

        SUBMITTED ops are included: the deadline applies from
        submission, regardless of whether dispatch ever picked the op up.
        """
        expired: list[PendingOperation] = []
        for op in self._operations.values():
            if (
                op.status in {OpStatus.SUBMITTED, OpStatus.PENDING}
                and op.deadline_tick is not None
                and current_tick > op.deadline_tick
            ):
                op.status = OpStatus.TIMED_OUT
                expired.append(op)
        return expired

    def collect_completed(
        self,
        current_tick: int,
    ) -> list[PendingOperation]:
        """Collect operations that are completed and eligible for processing.

        An operation is eligible when:
        - status is COMPLETED
        - eligible_tick <= current_tick
        """
        eligible: list[PendingOperation] = []
        for op in self._operations.values():
            if (
                op.status == OpStatus.COMPLETED
                and op.eligible_tick <= current_tick
            ):
                eligible.append(op)
        return eligible

    def get_by_agent(
        self,
        agent_id: str,
        status: OpStatus | None = None,
    ) -> list[PendingOperation]:
        """Get all operations for a specific agent, optionally filtered by status."""
        ops = [op for op in self._operations.values() if op.agent_id == agent_id]
        if status is not None:
            ops = [op for op in ops if op.status == status]
        return ops

    def get_by_id(
        self,
        request_id: str,
    ) -> PendingOperation | None:
        """Get an operation by its request ID."""
        return self._operations.get(request_id)

    def count_in_flight(
        self,
        agent_id: str,
        op_type: OpType | None = None,
    ) -> int:
        """Number of SUBMITTED/PENDING operations for an agent.

        Used by Phase 6 (Validate) to enforce per-agent budgets.
        """
        return sum(
            1 for op in self._operations.values()
            if op.agent_id == agent_id
            and op.status in {OpStatus.SUBMITTED, OpStatus.PENDING}
            and (op_type is None or op.op_type == op_type)
        )

    def find_in_flight_request_id(
        self,
        agent_id: str,
        request_id: str,
    ) -> PendingOperation | None:
        """Find an in-flight op whose intent request_id matches.

        Prevents an agent from reusing a request_id that is already in
        flight (Phase 6 Validate).
        """
        for op in self._operations.values():
            if (
                op.agent_id == agent_id
                and op.status in {OpStatus.SUBMITTED, OpStatus.PENDING}
                and op.metadata.get("request_id") == request_id
            ):
                return op
        return None

    def remove(self, request_id: str) -> PendingOperation | None:
        """Remove a single operation from the registry.

        Called after the simulation has consumed a completed result.
        Returns the removed operation, or None if not found.
        NOTE: seen_requests is NOT cleaned up here — consumed ops must
        remain in history to prevent replay.  Use remove_for_rollback()
        when the op was never legitimately consumed.
        """
        return self._operations.pop(request_id, None)

    def remove_for_rollback(self, request_id: str) -> PendingOperation | None:
        """Remove an operation AND its seen_requests entry (rollback).

        Used by _phase_commit._rollback() to undo this-tick registrations
        that were never consumed — the request_id becomes reusable.
        """
        op = self._operations.pop(request_id, None)
        if op is not None:
            req = op.metadata.get("request_id")
            if req and req in self._seen_requests:
                del self._seen_requests[req]
        return op

    def remove_completed(self) -> int:
        """Remove all completed/failed/cancelled/timed_out operations.

        Returns the number of operations removed.
        """
        to_remove = [
            rid for rid, op in self._operations.items()
            if op.status in {
                OpStatus.COMPLETED,
                OpStatus.FAILED,
                OpStatus.CANCELLED,
                OpStatus.TIMED_OUT,
            }
        ]
        for rid in to_remove:
            del self._operations[rid]
        return len(to_remove)

    @property
    def pending_count(self) -> int:
        """Number of operations still in flight."""
        return sum(
            1 for op in self._operations.values()
            if op.status in {OpStatus.SUBMITTED, OpStatus.PENDING}
        )

    @property
    def completed_count(self) -> int:
        """Number of completed operations awaiting processing."""
        return sum(
            1 for op in self._operations.values()
            if op.status == OpStatus.COMPLETED
        )

    def summary(self) -> dict[str, Any]:
        """Get a summary of all operations."""
        by_status: dict[str, int] = {}
        for op in self._operations.values():
            by_status[op.status.value] = by_status.get(op.status.value, 0) + 1
        return {
            "total": len(self._operations),
            "by_status": by_status,
        }
