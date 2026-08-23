"""Task tree management: CRUD, state tracking, and tree traversal.

Per SPEC §4.4, §11.2:
- Dynamic task tree (created/completed/failed during runtime)
- Task states with defined transitions
- Tree traversal (parent/children)
- Deadline checking
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from my_team.models.task import TASK_TRANSITIONS, Task, TaskPriority, TaskStatus


class TaskTreeError(Exception):
    """Base exception for task tree errors."""


class TaskNotFoundError(TaskTreeError):
    """Raised when a task is not found."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Task not found: '{task_id}'")


class InvalidTransitionError(TaskTreeError):
    """Raised when an invalid task state transition is attempted."""

    def __init__(self, task_id: str, from_status: TaskStatus, to_status: TaskStatus) -> None:
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Invalid transition for '{task_id}': "
            f"{from_status.value} → {to_status.value}"
        )


class TaskTree:
    """Manages the dynamic task tree.

    Provides CRUD operations, state transitions, tree traversal,
    and deadline checking.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._children_map: dict[str, list[str]] = {}  # parent_task_id → [child_ids]
        self._parent_map: dict[str, str | None] = {}   # task_id → parent_task_id
        self._assignee_map: dict[str, list[str]] = {}      # agent_id → [task_ids]

    def create(
        self,
        task_id: str,
        title: str,
        assigner_agent_id: str,
        assignee_agent_id: str,
        description: str = "",
        parent_task_id: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        deadline: datetime | None = None,
        required_outputs: list[str] | None = None,
        tick: int = 0,
        **kwargs: Any,
    ) -> Task:
        """Create a new task and add it to the tree.

        Args:
            task_id: Unique task identifier.
            title: Task title.
            assigner_agent_id: Agent that created the task.
            assignee_agent_id: Agent responsible for execution.
            parent_task_id: Parent task (None for root tasks).
            priority: Task priority.
            deadline: Real-calendar completion deadline (SPEC §9.1).
            required_outputs: Expected output descriptions.
            tick: Current simulation tick.

        Returns:
            The created Task.
        """
        if task_id in self._tasks:
            raise TaskTreeError(f"Task '{task_id}' already exists")

        if parent_task_id is not None and parent_task_id not in self._tasks:
            raise TaskNotFoundError(parent_task_id)

        task = Task(
            task_id=task_id,
            title=title,
            description=description,
            assigner_agent_id=assigner_agent_id,
            assignee_agent_id=assignee_agent_id,
            parent_task_id=parent_task_id,
            priority=priority,
            deadline=deadline,
            required_outputs=required_outputs or [],
            created_at_tick=tick,
            updated_at_tick=tick,
            **kwargs,
        )

        self._tasks[task_id] = task
        self._parent_map[task_id] = parent_task_id

        # Update parent's children list
        if parent_task_id is not None:
            if parent_task_id not in self._children_map:
                self._children_map[parent_task_id] = []
            self._children_map[parent_task_id].append(task_id)

        # Update owner map
        if assignee_agent_id not in self._assignee_map:
            self._assignee_map[assignee_agent_id] = []
        self._assignee_map[assignee_agent_id].append(task_id)

        return task

    def get(self, task_id: str) -> Task:
        """Get a task by ID."""
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        return self._tasks[task_id]

    def exists(self, task_id: str) -> bool:
        return task_id in self._tasks

    def update_status(
        self,
        task_id: str,
        target_status: TaskStatus,
        tick: int = 0,
        allow_walk: bool = False,
    ) -> Task:
        """Transition a task to a new status.

        By default the transition must be directly allowed. When
        allow_walk=True, walks through intermediate states when the
        direct transition is not allowed (e.g. assigned → completed
        goes through accepted → in_progress → submitted → completed).
        Raises InvalidTransitionError if the target is unreachable.
        """
        task = self.get(task_id)
        if task.can_transition_to(target_status):
            task.transition_to(target_status, tick)
            return task

        if not allow_walk:
            raise InvalidTransitionError(task_id, task.status, target_status)

        # Walk the transition graph to find a path to the target
        from collections import deque
        visited = {task.status}
        queue: deque[tuple[TaskStatus, list[TaskStatus]]] = deque(
            [(task.status, [])]
        )
        while queue:
            current, path = queue.popleft()
            for nxt in TASK_TRANSITIONS.get(current, set()):
                if nxt in visited:
                    continue
                visited.add(nxt)
                new_path = path + [nxt]
                if nxt == target_status:
                    for step in new_path:
                        task.transition_to(step, tick)
                    return task
                queue.append((nxt, new_path))

        raise InvalidTransitionError(task_id, task.status, target_status)

    def add_child(self, parent_task_id: str, child_task_id: str) -> None:
        """Register a parent-child relationship."""
        if parent_task_id not in self._tasks:
            raise TaskNotFoundError(parent_task_id)
        if child_task_id not in self._tasks:
            raise TaskNotFoundError(child_task_id)

        if parent_task_id not in self._children_map:
            self._children_map[parent_task_id] = []
        if child_task_id not in self._children_map[parent_task_id]:
            self._children_map[parent_task_id].append(child_task_id)
        self._parent_map[child_task_id] = parent_task_id

    def children(self, task_id: str) -> list[Task]:
        """Get direct child tasks."""
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        child_ids = self._children_map.get(task_id, [])
        return [self._tasks[cid] for cid in child_ids if cid in self._tasks]

    def child_ids(self, task_id: str) -> list[str]:
        """Get direct child task IDs."""
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        return list(self._children_map.get(task_id, []))

    def parent(self, task_id: str) -> Task | None:
        """Get parent task, or None for root tasks."""
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        parent_id = self._parent_map.get(task_id)
        if parent_id is None:
            return None
        return self._tasks.get(parent_id)

    def get_assignee_tasks(self, agent_id: str) -> list[Task]:
        """Get all tasks owned by an agent."""
        task_ids = self._assignee_map.get(agent_id, [])
        return [self._tasks[tid] for tid in task_ids if tid in self._tasks]

    def get_active_tasks(self) -> list[Task]:
        """Get all tasks in active (non-terminal) states."""
        return [t for t in self._tasks.values() if t.is_active]

    def get_expired_tasks(self, now: datetime) -> list[Task]:
        """Get all tasks whose real-calendar deadline has passed (SPEC §9.1).

        Args:
            now: Current business wall-clock time (engine.wall_now()).
        """
        expired: list[Task] = []
        for task in self._tasks.values():
            if (
                task.deadline is not None
                and now > task.deadline
                and not task.is_terminal
            ):
                expired.append(task)
        return expired

    def expire_task(self, task_id: str, tick: int) -> Task:
        """Mark a task as expired."""
        task = self.get(task_id)
        if not task.is_terminal:
            task.transition_to(TaskStatus.EXPIRED, tick)
        return task

    def cancel_task(self, task_id: str, tick: int) -> list[Task]:
        """Cancel a task and cascade to all non-terminal children.

        Per review gap: parent cancel → child cascade.
        Returns list of all tasks that were cancelled (including the root).
        Already-completed or already-cancelled children are skipped.
        """
        cancelled: list[Task] = []
        stack = [task_id]
        while stack:
            tid = stack.pop()
            if tid not in self._tasks:
                continue
            task = self._tasks[tid]
            if not task.is_terminal:
                task.transition_to(TaskStatus.CANCELLED, tick)
                cancelled.append(task)
            # Queue children for cascade (even if parent was already terminal,
            # we still check children for non-terminal state)
            stack.extend(self._child_map_children(tid))
        return cancelled

    def subtree(self, task_id: str) -> list[Task]:
        """Get all tasks in the subtree rooted at task_id (including self)."""
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        result: list[Task] = []
        stack = [task_id]
        while stack:
            node = stack.pop()
            result.append(self._tasks[node])
            stack.extend(self._child_map_children(node))
        return result

    def _child_map_children(self, task_id: str) -> list[str]:
        return self._children_map.get(task_id, [])

    def ancestors(self, task_id: str) -> list[Task]:
        """Get all ancestor tasks from parent up to root."""
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        result: list[Task] = []
        current = self._parent_map.get(task_id)
        while current is not None:
            result.append(self._tasks[current])
            current = self._parent_map.get(current)
        return result

    def is_ancestor(self, ancestor_id: str, descendant_id: str) -> bool:
        """Check if ancestor_id is an ancestor of descendant_id."""
        if ancestor_id not in self._tasks or descendant_id not in self._tasks:
            return False
        current: str | None = descendant_id
        while current is not None:
            if current == ancestor_id:
                return True
            current = self._parent_map.get(current)
        return False

    def count(self) -> int:
        return len(self._tasks)

    def all_ids(self) -> list[str]:
        return list(self._tasks.keys())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the task tree."""
        return {
            "tasks": [task.model_dump() for task in self._tasks.values()],
            "count": len(self._tasks),
        }

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._tasks

    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self):
        return iter(self._tasks.values())

    def __repr__(self) -> str:
        return f"TaskTree({len(self._tasks)} tasks)"
