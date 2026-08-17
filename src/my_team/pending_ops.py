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
    ) -> PendingOperation:
        """Register a new pending operation."""
        op = PendingOperation(
            op_type=op_type,
            agent_id=agent_id,
            created_tick=created_tick,
            eligible_tick=eligible_tick or created_tick + 1,
            deadline_tick=deadline_tick,
            task_id=task_id,
            activation_id=activation_id,
            metadata=metadata or {},
        )
        self._operations[op.request_id] = op
        return op

    def complete(
        self,
        request_id: str,
        result: dict[str, Any] | None = None,
    ) -> PendingOperation | None:
        """Mark an operation as completed."""
        op = self._operations.get(request_id)
        if op is None:
            return None
        op.status = OpStatus.COMPLETED
        op.result = result or {}
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
        """Cancel an operation."""
        op = self._operations.get(request_id)
        if op is None:
            return None
        op.status = OpStatus.CANCELLED
        return op

    def timeout_expired(
        self,
        current_tick: int,
    ) -> list[PendingOperation]:
        """Find operations that have exceeded their deadline."""
        expired: list[PendingOperation] = []
        for op in self._operations.values():
            if (
                op.status == OpStatus.PENDING
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
