"""Delegation protocol for inter-agent task delegation.

Per SPEC §7.1, §7.2, §7.3:
- Delegation only to direct children via email
- Full protocol: delegation → acceptance/failure → work → result
- Sub-tasks must be children of delegator's current task
- Permission transfer: child_effective ⊆ delegator_effective
"""

from __future__ import annotations

from typing import Any

from my_team.agent_tree import AgentTree
from my_team.mailbox import MailSystem
from my_team.models.email import Email, EmailType
from my_team.models.task import Task, TaskPriority, TaskStatus
from my_team.task_tree import TaskTree


class DelegationError(Exception):
    """Base exception for delegation errors."""


class NotDirectChildError(DelegationError):
    """Raised when trying to delegate to a non-child agent."""

    def __init__(self, delegator_id: str, target_id: str) -> None:
        self.delegator_id = delegator_id
        self.target_id = target_id
        super().__init__(
            f"Agent '{delegator_id}' cannot delegate to '{target_id}' "
            f"(not a direct child)"
        )


class DelegationDepthError(DelegationError):
    """Raised when delegation would exceed max depth."""

    def __init__(self, task_id: str, depth: int, max_depth: int) -> None:
        super().__init__(
            f"Delegation for task '{task_id}' would exceed max depth "
            f"({depth} > {max_depth})"
        )


class DelegationDeadlineError(DelegationError):
    """Raised when sub-task deadline exceeds parent deadline."""

    def __init__(self, sub_deadline: int, parent_deadline: int) -> None:
        super().__init__(
            f"Sub-task deadline ({sub_deadline}) exceeds "
            f"parent deadline ({parent_deadline})"
        )


class DelegationProtocol:
    """Implements the complete delegation protocol between agents.

    Manages the lifecycle of delegated tasks and the email-based
    communication that accompanies them.
    """

    def __init__(
        self,
        agent_tree: AgentTree,
        task_tree: TaskTree,
        mail_system: MailSystem,
        max_delegation_depth: int = 5,
    ) -> None:
        self._agent_tree = agent_tree
        self._task_tree = task_tree
        self._mail = mail_system
        self._max_depth = max_delegation_depth

    def delegate(
        self,
        delegator_id: str,
        target_id: str,
        title: str,
        description: str = "",
        parent_task_id: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        deadline_tick: int | None = None,
        required_outputs: list[str] | None = None,
        tick: int = 0,
        instructions: str = "",
        **kwargs: Any,
    ) -> tuple[Task, Email]:
        """Delegate a task from one agent to another.

        Creates a child task and sends a delegation email.

        Args:
            delegator_id: Agent doing the delegating.
            target_id: Agent receiving the delegation (must be direct child).
            title: Task title.
            description: Task description.
            parent_task_id: Parent task ID (must belong to delegator).
            priority: Task priority.
            deadline_tick: Sub-task deadline.
            required_outputs: Expected outputs.
            tick: Current simulation tick.
            instructions: Additional instructions for the delegate.

        Returns:
            Tuple of (created Task, delegation Email).

        Raises:
            NotDirectChildError: If target is not a direct child.
            DelegationDepthError: If delegation would exceed max depth.
            DelegationDeadlineError: If sub-task deadline exceeds parent.
        """
        # Validate delegation target is a direct child
        if not self._agent_tree.can_delegate_to(delegator_id, target_id):
            raise NotDirectChildError(delegator_id, target_id)

        # Check delegation depth
        if parent_task_id is not None:
            ancestors = self._task_tree.ancestors(parent_task_id)
            depth = len(ancestors) + 1
            if depth > self._max_depth:
                raise DelegationDepthError(
                    f"{delegator_id}:{parent_task_id}",
                    depth,
                    self._max_depth,
                )

            # Check deadline constraint
            parent_task = self._task_tree.get(parent_task_id)
            if (
                parent_task.deadline_tick is not None
                and deadline_tick is not None
                and deadline_tick > parent_task.deadline_tick
            ):
                raise DelegationDeadlineError(deadline_tick, parent_task.deadline_tick)

        # Generate sub-task ID
        sub_task_id = f"{parent_task_id or 'root'}.{target_id.split('.')[-1]}.{tick}"

        # Create the sub-task
        task = self._task_tree.create(
            task_id=sub_task_id,
            title=title,
            creator_agent_id=delegator_id,
            owner_agent_id=target_id,
            description=description,
            parent_task_id=parent_task_id,
            priority=priority,
            deadline_tick=deadline_tick,
            required_outputs=required_outputs,
            tick=tick,
        )

        # Transition to ASSIGNED
        task.transition_to(TaskStatus.ASSIGNED, tick)

        # Build delegation email
        email_body = (
            f"Task: {title}\n"
            f"Description: {description}\n"
            f"Priority: {priority.value}\n"
        )
        if deadline_tick is not None:
            email_body += f"Deadline: tick {deadline_tick}\n"
        if instructions:
            email_body += f"\nInstructions:\n{instructions}\n"

        # Convert TaskPriority to EmailPriority
        from my_team.models.email import EmailPriority
        priority_map = {
            TaskPriority.LOW: EmailPriority.LOW,
            TaskPriority.NORMAL: EmailPriority.NORMAL,
            TaskPriority.HIGH: EmailPriority.HIGH,
            TaskPriority.URGENT: EmailPriority.URGENT,
        }
        email_priority = priority_map.get(priority, EmailPriority.NORMAL)

        email = self._mail.create_email(
            from_agent=delegator_id,
            to=[target_id],
            subject=f"[DELEGATE] {title}",
            body=email_body,
            email_type=EmailType.DELEGATION,
            task_id=sub_task_id,
            tick=tick,
            deliver_at_tick=tick + 1,
            priority=email_priority,
            requires_reply=True,
        )

        return task, email

    def accept(
        self,
        agent_id: str,
        task_id: str,
        tick: int = 0,
        estimated_completion_tick: int | None = None,
    ) -> Email:
        """Accept a delegated task.

        Transitions task to ACCEPTED and sends acceptance email.
        """
        task = self._task_tree.update_status(task_id, TaskStatus.ACCEPTED, tick)

        body = "Task accepted."
        if estimated_completion_tick is not None:
            body += f" Estimated completion: tick {estimated_completion_tick}"

        # Find the delegator (task creator)
        delegator_id = task.creator_agent_id

        email = self._mail.create_email(
            from_agent=agent_id,
            to=[delegator_id],
            subject=f"[ACCEPT] {task.title}",
            body=body,
            email_type=EmailType.ACCEPTANCE,
            task_id=task_id,
            tick=tick,
            deliver_at_tick=tick + 1,
        )
        return email

    def reject(
        self,
        agent_id: str,
        task_id: str,
        reason: str = "",
        tick: int = 0,
    ) -> Email:
        """Reject a delegated task.

        Transitions task to FAILED and sends failure email.
        """
        task = self._task_tree.update_status(task_id, TaskStatus.FAILED, tick)

        delegator_id = task.creator_agent_id

        email = self._mail.create_email(
            from_agent=agent_id,
            to=[delegator_id],
            subject=f"[REJECT] {task.title}",
            body=f"Task rejected. Reason: {reason}",
            email_type=EmailType.FAILURE,
            task_id=task_id,
            tick=tick,
            deliver_at_tick=tick + 1,
        )
        return email

    def report_progress(
        self,
        agent_id: str,
        task_id: str,
        message: str,
        tick: int = 0,
    ) -> Email:
        """Report progress on a task."""
        task = self._task_tree.get(task_id)
        delegator_id = task.creator_agent_id

        email = self._mail.create_email(
            from_agent=agent_id,
            to=[delegator_id],
            subject=f"[PROGRESS] {task.title}",
            body=message,
            email_type=EmailType.PROGRESS,
            task_id=task_id,
            tick=tick,
            deliver_at_tick=tick + 1,
        )
        return email

    def report_blocked(
        self,
        agent_id: str,
        task_id: str,
        reason: str,
        tick: int = 0,
    ) -> Email:
        """Report that a task is blocked."""
        task = self._task_tree.update_status(task_id, TaskStatus.BLOCKED, tick)
        delegator_id = task.creator_agent_id

        email = self._mail.create_email(
            from_agent=agent_id,
            to=[delegator_id],
            subject=f"[BLOCKED] {task.title}",
            body=f"Task blocked. Reason: {reason}",
            email_type=EmailType.BLOCKED,
            task_id=task_id,
            tick=tick,
            deliver_at_tick=tick + 1,
        )
        return email

    def submit_result(
        self,
        agent_id: str,
        task_id: str,
        summary: str,
        artifacts: list[dict[str, Any]] | None = None,
        limitations: list[str] | None = None,
        recommendation: str = "",
        tick: int = 0,
    ) -> Email:
        """Submit work results for a task.

        Transitions task to SUBMITTED and sends result email.
        """
        task = self._task_tree.update_status(task_id, TaskStatus.SUBMITTED, tick)
        delegator_id = task.creator_agent_id

        body = f"Summary: {summary}\n"
        if limitations:
            body += f"Limitations: {'; '.join(limitations)}\n"
        if recommendation:
            body += f"Recommendation: {recommendation}\n"

        email = self._mail.create_email(
            from_agent=agent_id,
            to=[delegator_id],
            subject=f"[RESULT] {task.title}",
            body=body,
            email_type=EmailType.RESULT,
            task_id=task_id,
            tick=tick,
            deliver_at_tick=tick + 1,
            metadata={"artifacts": artifacts or []},
        )
        return email

    def cancel(
        self,
        agent_id: str,
        task_id: str,
        reason: str = "",
        tick: int = 0,
    ) -> Email:
        """Cancel a task."""
        task = self._task_tree.update_status(task_id, TaskStatus.CANCELLED, tick)

        # Notify the owner
        owner_id = task.owner_agent_id
        email = self._mail.create_email(
            from_agent=agent_id,
            to=[owner_id],
            subject=f"[CANCEL] {task.title}",
            body=f"Task cancelled. Reason: {reason}" if reason else "Task cancelled.",
            email_type=EmailType.CANCELLATION,
            task_id=task_id,
            tick=tick,
            deliver_at_tick=tick + 1,
        )
        return email

    def check_expired(self, current_tick: int) -> list[Task]:
        """Check for and expire overdue tasks."""
        expired = self._task_tree.get_expired_tasks(current_tick)
        for task in expired:
            self._task_tree.expire_task(task.task_id, current_tick)
        return expired
