"""Task tree management: CRUD, state tracking, and tree traversal.

Per SPEC §4.4, §11.2:
- Dynamic task tree (created/completed/failed during runtime)
- Task states with defined transitions
- Tree traversal (parent/children)
- Deadline checking

N1c-4: TaskTree 归位为 Device 子类（SPEC §5.7，N1c Task 设备公共数据层）。
注册受控 uuid（范围级 DATA + delegate TOOL）+ InjectionDecl。
构造签名保持完全兼容（simulation.py 不变，位置参数 transaction_buffer 仍可用）。
细粒度 position 求值留 N5（§4 / §6）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from my_team.devices.base import Device, EntityKind, InjectionDecl
from my_team.models.task import TASK_TRANSITIONS, Task, TaskPriority, TaskStatus

if TYPE_CHECKING:
    from my_team.agent_runtime import ToolContext
    from my_team.transaction import TransactionBuffer


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
            f"Invalid transition for '{task_id}': {from_status.value} → {to_status.value}"
        )


class TaskTree(Device):
    """Manages the dynamic task tree.

    Provides CRUD operations, state transitions, tree traversal,
    and deadline checking.

    N1c-4 设备归位：继承 Device，构造时注册受控 uuid
    （范围级 DATA task-tree-scope + delegate TOOL 实体）并声明 InjectionDecl。
    构造签名保持原样：``transaction_buffer`` 仍为第一个位置参数；
    新增 ``device_id`` 可选关键字参数（默认 None，simulation 构造不变）。
    细粒度 position 求值留 N5。
    """

    def __init__(
        self,
        transaction_buffer: TransactionBuffer | None = None,
        device_id: str | None = None,
    ) -> None:
        # Device 基类初始化（生成 device_id 并初始化实体注册表）
        Device.__init__(self, device_id)
        self._tasks: dict[str, Task] = {}
        self._children_map: dict[str, list[str]] = {}  # derived_from → [child_ids]
        self._parent_map: dict[str, str | None] = {}  # task_id → derived_from
        self._assignee_map: dict[str, list[str]] = {}  # agent_id → [task_ids]
        # N1c-2: injected kernel services for tool handlers
        self._transaction_buffer = transaction_buffer

        # N1c-4：注册设备受控实体
        # 范围级 DATA 实体 — 任务树整体范围，InjectionDecl 引导 agent 使用 delegate 工具
        self.task_tree_scope_id = self.register_entity(
            EntityKind.DATA,
            "task-tree-scope",
            injection=InjectionDecl(
                content=(
                    "[TASK_INSTRUCTION] 任务树（TaskTree）是团队任务委派与跟踪的数据层。\n"
                    "通过 delegate 工具将任务委派给直属子 agent（STAGED_MUTATION：\n"
                    "原子组 TASK_CREATE + EMAIL_SEND，提交时生效）。\n"
                    "任务有生命周期状态（assigned → accepted → in_progress → "
                    "submitted → completed / failed / cancelled / expired）；\n"
                    "parent cancel 会级联取消所有非终态子任务。"
                ),
                source_tag="[TASK_INSTRUCTION]",
            ),
        )
        # 工具面 TOOL 实体 — delegate 工具，采用 uuid5 派生值（adopt 机制）
        from my_team.tool_manifest import builtin_manifests

        _manifests = builtin_manifests()
        self.delegate_capability = self.register_entity(
            EntityKind.TOOL,
            "delegate",
            entity_id=_manifests["delegate"].capability,
        )

    def create(
        self,
        task_id: str,
        title: str,
        assigner_agent_id: str,
        assignee_agent_id: str,
        description: str = "",
        derived_from: str | None = None,
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
            derived_from: Task this copy derives from (None for root tasks).
            priority: Task priority.
            deadline: Real-calendar completion deadline (SPEC §9.1).
            required_outputs: Expected output descriptions.
            tick: Current simulation tick.

        Returns:
            The created Task.
        """
        if task_id in self._tasks:
            raise TaskTreeError(f"Task '{task_id}' already exists")

        if derived_from is not None and derived_from not in self._tasks:
            raise TaskNotFoundError(derived_from)

        task = Task(
            task_id=task_id,
            title=title,
            description=description,
            assigner_agent_id=assigner_agent_id,
            assignee_agent_id=assignee_agent_id,
            derived_from=derived_from,
            priority=priority,
            deadline=deadline,
            required_outputs=required_outputs or [],
            created_at_tick=tick,
            updated_at_tick=tick,
            **kwargs,
        )

        self._tasks[task_id] = task
        self._parent_map[task_id] = derived_from

        # Update parent's children list
        if derived_from is not None:
            if derived_from not in self._children_map:
                self._children_map[derived_from] = []
            self._children_map[derived_from].append(task_id)

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
        queue: deque[tuple[TaskStatus, list[TaskStatus]]] = deque([(task.status, [])])
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

    def add_child(self, derived_from: str, child_task_id: str) -> None:
        """Register a parent-child relationship."""
        if derived_from not in self._tasks:
            raise TaskNotFoundError(derived_from)
        if child_task_id not in self._tasks:
            raise TaskNotFoundError(child_task_id)

        if derived_from not in self._children_map:
            self._children_map[derived_from] = []
        if child_task_id not in self._children_map[derived_from]:
            self._children_map[derived_from].append(child_task_id)
        self._parent_map[child_task_id] = derived_from

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
            if task.deadline is not None and now > task.deadline and not task.is_terminal:
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

    # -----------------------------------------------------------------------
    # N1c-2: Tool handler factory (delegate)
    # -----------------------------------------------------------------------

    def make_handle_delegate(self) -> Callable[..., Any]:
        """Return the ``delegate`` tool handler bound to this task tree.

        Creates task + sends delegation email — staged as two effects in
        ONE atomic group (task fails → email must not be sent).
        """
        from my_team.agent_runtime import ToolResult

        transaction_buffer = self._transaction_buffer

        def handle_delegate(
            context: ToolContext,
            recipient_agent_id: str = "",
            task_title: str = "",
            task_description: str = "",
            **_kw: Any,
        ) -> Any:
            from uuid import uuid4

            task_id = f"task.{context.tick}.{uuid4().hex[:8]}"
            group_id = f"group.{uuid4().hex[:8]}"
            if transaction_buffer is not None:
                from my_team.transaction import EffectType

                transaction_buffer.stage(
                    effect_type=EffectType.TASK_CREATE,
                    agent_id=context.agent_id,
                    resource=task_id,
                    data={
                        "task_id": task_id,
                        "title": task_title,
                        "description": task_description,
                        "assigner_agent_id": context.agent_id,
                        "assignee_agent_id": recipient_agent_id,
                        "derived_from": None,
                    },
                    group_id=group_id,
                    atomicity="group",
                )
                transaction_buffer.stage(
                    effect_type=EffectType.EMAIL_SEND,
                    agent_id=context.agent_id,
                    resource=f"email:{context.agent_id}",
                    data={
                        "from_agent": context.agent_id,
                        "to": [recipient_agent_id],
                        "subject": f"[DELEGATE] {task_title}",
                        "body": task_description,
                        "email_type": "delegation",
                        "task_id": task_id,
                    },
                    group_id=group_id,
                    atomicity="group",
                )
            return ToolResult(
                success=True,
                data={"task_id": task_id, "staged": True},
                agent_id=context.agent_id,
                tool_name="delegate",
                tick=context.tick,
            )

        return handle_delegate
