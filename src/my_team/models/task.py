"""Task data model for the dynamic task tree.

Per SPEC §4.4, §11.2:
- Tasks are units of work with a full lifecycle
- Task tree is dynamic (created/completed/failed during runtime)
- Each task's executor must match org tree authorization
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Task lifecycle states per SPEC §4.4."""

    DRAFT = "draft"
    ASSIGNED = "assigned"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    WAITING_FOR_CHILDREN = "waiting_for_children"
    SUBMITTED = "submitted"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TaskPriority(str, Enum):
    """Task priority levels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# Valid state transitions for tasks
TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.DRAFT: {TaskStatus.ASSIGNED, TaskStatus.CANCELLED},
    TaskStatus.ASSIGNED: {
        TaskStatus.ACCEPTED, TaskStatus.FAILED,
        TaskStatus.CANCELLED, TaskStatus.EXPIRED,
    },
    TaskStatus.ACCEPTED: {
        TaskStatus.IN_PROGRESS, TaskStatus.FAILED,
        TaskStatus.CANCELLED, TaskStatus.EXPIRED,
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.SUBMITTED,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_FOR_CHILDREN,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.EXPIRED,
    },
    TaskStatus.BLOCKED: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.EXPIRED,
    },
    TaskStatus.WAITING_FOR_CHILDREN: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.SUBMITTED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.EXPIRED,
    },
    TaskStatus.SUBMITTED: {TaskStatus.REVIEWING, TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.REVIEWING: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.IN_PROGRESS},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
    TaskStatus.EXPIRED: set(),
}


class TaskArtifact(BaseModel):
    """Reference to a task output artifact."""

    artifact_type: str = Field(description="Type: shared_kb_file, private_file, email_ref")
    path: str = Field(description="Path to the artifact")
    version: int = Field(default=0, description="Version number (for shared KB)")
    description: str = Field(default="", description="Human-readable description")


class Task(BaseModel):
    """A unit of work in the system, per SPEC §4.4."""

    task_id: str = Field(description="Unique identifier, e.g. 'task.2026.001'")
    title: str = Field(description="Task title")
    description: str = Field(default="", description="Detailed task description")
    creator_agent_id: str = Field(description="Agent that created the task")
    owner_agent_id: str = Field(description="Agent currently responsible")
    parent_task_id: str | None = Field(
        default=None,
        description="Parent task ID (None for root tasks)",
    )
    child_task_ids: list[str] = Field(
        default_factory=list,
        description="Child task IDs",
    )
    status: TaskStatus = Field(default=TaskStatus.DRAFT)
    priority: TaskPriority = Field(default=TaskPriority.NORMAL)
    deadline: datetime | None = Field(
        default=None,
        description=(
            "Real-calendar completion deadline (SPEC §9.1 时间模型 — "
            "business layer is always wall-clock; no tick fields)"
        ),
    )
    derived_from: str | None = Field(
        default=None,
        description=(
            "Task this copy was derived from (delegation creates a copy; "
            "SPEC §4.2 委派=建副本). The task tree view follows these "
            "references along the delegation chain."
        ),
    )
    required_outputs: list[str] = Field(
        default_factory=list,
        description="Expected output descriptions",
    )
    artifacts: list[TaskArtifact] = Field(
        default_factory=list,
        description="Submitted artifacts",
    )
    created_at_tick: int = Field(default=0)
    updated_at_tick: int = Field(default=0)
    completed_at_tick: int | None = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def can_transition_to(self, target: TaskStatus) -> bool:
        """Check if a transition to the target status is allowed."""
        return target in TASK_TRANSITIONS.get(self.status, set())

    def transition_to(self, target: TaskStatus, tick: int = 0) -> None:
        """Execute a status transition. Raises ValueError if invalid."""
        if not self.can_transition_to(target):
            raise ValueError(
                f"Invalid task transition: {self.status.value} → {target.value}"
            )
        self.status = target
        self.updated_at_tick = tick
        if target == TaskStatus.COMPLETED:
            self.completed_at_tick = tick

    @property
    def is_terminal(self) -> bool:
        """Check if the task is in a terminal state."""
        return self.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        }

    @property
    def is_active(self) -> bool:
        """Check if the task is actively being worked on."""
        return self.status in {
            TaskStatus.ASSIGNED,
            TaskStatus.ACCEPTED,
            TaskStatus.IN_PROGRESS,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_FOR_CHILDREN,
        }
