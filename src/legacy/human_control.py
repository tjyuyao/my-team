"""Human control interface for the simulation.

Per SPEC §12:
- Pause/resume simulation
- Send emails to any agent
- View system status
- Adjust tick duration
- All operations audit-logged

T12a (SPEC §10.1): human WORKER actions — accept/complete/fail —
ingress as ``human``-source IngressEvents and go through the same
transaction path as AI worker intents (no separate channel).
"""

from __future__ import annotations

from typing import Any

from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType, AuditLog
from my_team.ingress import IngressBuffer, IngressEvent
from my_team.mailbox import MailSystem
from my_team.models.email import EmailPriority, EmailType
from my_team.shared_kb import SharedKB
from my_team.task_tree import TaskTree
from my_team.tick_engine import SimulationState, TickEngine
from pydantic import BaseModel, Field


class HumanCommand(BaseModel):
    """A command from a human operator."""

    command: str = Field(description="Command name")
    params: dict[str, Any] = Field(default_factory=dict)
    human_id: str = Field(default="human.user_001")


class CommandResult(BaseModel):
    """Result of executing a human command."""

    success: bool
    command: str
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class HumanControl:
    """Provides the human control plane for the simulation.

    Manages pause/resume, human email, status viewing, and
    tick duration adjustments.
    """

    def __init__(
        self,
        tick_engine: TickEngine,
        agent_tree: AgentTree,
        task_tree: TaskTree,
        mail_system: MailSystem,
        shared_kb: SharedKB,
        audit_log: AuditLog,
        ingress: IngressBuffer | None = None,  # T12a: human UI action ingress
    ) -> None:
        self._engine = tick_engine
        self._agent_tree = agent_tree
        self._task_tree = task_tree
        self._mail = mail_system
        self._kb = shared_kb
        self._audit = audit_log
        self._ingress = ingress
        self._pending_duration_changes: list[dict[str, Any]] = []

    # -- Pause / Resume (§12.1, §12.2) --------------------------------------

    def pause(self, reason: str = "", human_id: str = "human.user_001") -> CommandResult:
        """Pause the simulation.

        Takes effect after current tick commit (§12.1).
        Also handles CREATED state (prevent first advance).

        Pause semantics (§12.1):
        - Simulation ticks stop advancing
        - Lock leases do NOT advance (tick-based, not wall-clock)
        - Task deadlines do NOT advance (tick-based)
        - Email delivery does NOT occur (Deliver phase skipped)
        - Timeout checker does NOT run
        - All time-dependent state is frozen
        """
        if self._engine.state == SimulationState.PAUSED:
            return CommandResult(
                success=False, command="pause",
                message="Simulation is already paused",
            )

        if self._engine.state == SimulationState.CREATED:
            # Prevent the engine from ever starting
            self._engine._state = SimulationState.PAUSED
        else:
            self._engine.pause()

        self._audit.record(
            AuditEventType.HUMAN_PAUSE,
            details={"reason": reason, "human_id": human_id},
        )
        return CommandResult(
            success=True, command="pause",
            message="Simulation paused",
        )

    def resume(self, human_id: str = "human.user_001") -> CommandResult:
        """Resume the simulation from paused state (§12.2)."""
        if self._engine.state != SimulationState.PAUSED:
            return CommandResult(
                success=False, command="resume",
                message=f"Cannot resume: state is {self._engine.state.value}",
            )

        self._engine.resume()
        self._audit.record(
            AuditEventType.HUMAN_RESUME,
            details={"human_id": human_id},
        )
        return CommandResult(
            success=True, command="resume",
            message="Simulation resumed",
        )

    # -- Human Email (§12.4) ------------------------------------------------

    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str = "",
        human_id: str = "human.user_001",
        tick: int | None = None,
        deliver_at_tick: int | None = None,
        priority: EmailPriority = EmailPriority.NORMAL,
    ) -> CommandResult:
        """Send an email from a human to agents (§12.4).

        The email enters the agent's normal mailbox. It cannot bypass
        permissions to directly modify agent state.
        """
        # Validate recipients exist
        for agent_id in to:
            if agent_id not in self._agent_tree:
                return CommandResult(
                    success=False, command="send_email",
                    message=f"Agent '{agent_id}' not found",
                )

        current_tick = tick or self._engine.current_tick

        email = self._mail.create_email(
            from_agent=human_id,
            to=to,
            subject=subject,
            body=body,
            email_type=EmailType.HUMAN_MESSAGE,
            tick=current_tick,
            deliver_at_tick=deliver_at_tick if deliver_at_tick is not None else current_tick + 1,
            priority=priority,
        )

        self._audit.record(
            AuditEventType.HUMAN_EMAIL,
            details={
                "human_id": human_id,
                "to": to,
                "subject": subject,
                "email_id": email.email_id,
            },
        )

        return CommandResult(
            success=True, command="send_email",
            message=f"Email sent to {', '.join(to)}",
            data={"email_id": email.email_id},
        )

    # -- Tick Duration (§12.3) ----------------------------------------------

    def set_tick_duration(
        self,
        value: int,
        unit: str = "seconds",
        effective_tick: int | None = None,
        human_id: str = "human.user_001",
    ) -> CommandResult:
        """Adjust the tick duration (§12.3).

        If effective_tick is provided, the change takes effect at that tick.
        Otherwise, it takes effect at the next tick (immediate mode).
        """
        if value <= 0:
            return CommandResult(
                success=False, command="set_tick_duration",
                message="Duration must be positive",
            )

        if effective_tick is not None and effective_tick <= self._engine.current_tick:
            return CommandResult(
                success=False, command="set_tick_duration",
                message=f"effective_tick ({effective_tick}) must be in the future",
            )

        change = {
            "value": value,
            "unit": unit,
            "effective_tick": effective_tick,
        }

        if effective_tick is not None:
            self._pending_duration_changes.append(change)
            message = f"Tick duration will change to {value} {unit} at tick {effective_tick}"
        else:
            # Immediate: apply at next tick
            self._pending_duration_changes.append({
                **change,
                "effective_tick": self._engine.current_tick + 1,
            })
            message = f"Tick duration will change to {value} {unit} at next tick"

        self._audit.record(
            AuditEventType.HUMAN_CONFIG_CHANGE,
            details={
                "human_id": human_id,
                "setting": "tick_duration",
                "value": value,
                "unit": unit,
                "effective_tick": effective_tick,
            },
        )

        return CommandResult(
            success=True, command="set_tick_duration",
            message=message,
        )

    def apply_pending_duration_changes(self) -> None:
        """Apply any pending tick duration changes. Called by engine at tick start."""
        remaining: list[dict[str, Any]] = []
        for change in self._pending_duration_changes:
            if change["effective_tick"] <= self._engine.current_tick:
                self._engine.config.tick_duration_value = change["value"]
                self._engine.config.tick_duration_unit = change["unit"]
            else:
                remaining.append(change)
        self._pending_duration_changes = remaining

    # -- Status View (§16.5, §16.6, §16.7) ----------------------------------

    def view_agent_tree(self) -> dict[str, Any]:
        """View the organization tree (§16.5)."""
        return self._agent_tree.to_dict()

    def view_task_tree(self) -> dict[str, Any]:
        """View the task tree (§16.6)."""
        return self._task_tree.to_dict()

    def view_locks(self) -> dict[str, Any]:
        """View shared KB locks (§16.7)."""
        active = self._kb.locks.active_locks()
        return {
            "locks": [lock.model_dump() for lock in active],
            "count": len(active),
        }

    def view_simulation_status(self) -> dict[str, Any]:
        """View overall simulation status."""
        return {
            "tick": self._engine.current_tick,
            "state": self._engine.state.value,
            "agent_count": len(self._agent_tree),
            "task_count": self._task_tree.count(),
            "pending_emails": self._mail.pending_count,
            "active_locks": len(self._kb.locks),
            "tick_duration": {
                "value": self._engine.config.tick_duration_value,
                "unit": self._engine.config.tick_duration_unit,
            },
        }

    def view_agent_status(self, agent_id: str) -> dict[str, Any] | None:
        """View status of a specific agent."""
        if agent_id not in self._agent_tree:
            return None
        agent = self._agent_tree.get(agent_id)
        mailbox = self._mail.get_mailbox(agent_id)
        assignee_tasks = self._task_tree.get_assignee_tasks(agent_id)

        return {
            "agent_id": agent_id,
            "role": agent.role,
            "parent_id": agent.parent_id,
            "children": agent.children,
            "inbox_unread": mailbox.unread_count if mailbox else 0,
            "outbox_count": len(mailbox.outbox) if mailbox else 0,
            "active_tasks": len([t for t in assignee_tasks if t.is_active]),
            "completed_tasks": len([t for t in assignee_tasks if t.is_terminal]),
        }

    # -- Human Worker actions (T12a, SPEC §10.1) -----------------------------

    def submit_task_action(
        self,
        task_id: str,
        action: str,
        human_id: str = "human.user_001",
        **payload: Any,
    ) -> CommandResult:
        """A human worker's UI action: accept / complete / fail a task.

        The action is NOT applied directly — it is ingressed as an
        IngressEvent (source="human") and, on the next Ingest, routed to
        the task's kind=human assignee; HumanWorkerRuntime translates it
        to the corresponding Intent through the SAME transaction path as
        AI workers (Validate → Act → Commit). No separate channel.

        Returns a CommandResult (acceptance of the action for the
        ingress buffer; the task transition itself is async via the
        kernel tick).
        """
        if self._ingress is None:
            return CommandResult(
                success=False, command=f"task_{action}",
                message="No ingress buffer wired (human actions disabled)",
            )
        if action not in {"accept", "complete", "fail"}:
            return CommandResult(
                success=False, command="task_action",
                message=f"Unknown human action: {action!r}",
            )
        if not self._task_tree.exists(task_id):
            return CommandResult(
                success=False, command=f"task_{action}",
                message=f"Task '{task_id}' not found",
            )
        task = self._task_tree.get(task_id)
        cfg = self._agent_tree.get(task.assignee_agent_id)
        if cfg is None or cfg.kind != "human":
            return CommandResult(
                success=False, command=f"task_{action}",
                message=(
                    f"Task '{task_id}' assignee '{task.assignee_agent_id}' "
                    "is not a kind=human worker"
                ),
            )
        if task.is_terminal:
            return CommandResult(
                success=False, command=f"task_{action}",
                message=(
                    f"Task '{task_id}' is already terminal "
                    f"({task.status.value}) — cannot act on it"
                ),
            )
        event = IngressEvent(
            source="human",
            external_id=f"{task_id}:{action}",
            event_type="human_action",
            occurred_at=self._engine.wall_now().isoformat(),
            payload={
                "action": action,
                "task_id": task_id,
                "human_id": human_id,
                **payload,
            },
        )
        self._ingress.receive(event)
        self._audit.record(
            AuditEventType.HUMAN_ACTION,
            details={
                "human_id": human_id,
                "action": action,
                "task_id": task_id,
                "assignee": task.assignee_agent_id,
                "source": "human",
            },
        )
        return CommandResult(
            success=True, command=f"task_{action}",
            message=f"Human action '{action}' accepted for task '{task_id}'",
        )

    def accept_task(
        self,
        task_id: str,
        human_id: str = "human.user_001",
    ) -> CommandResult:
        """Human worker accepts an assigned task."""
        return self.submit_task_action(
            task_id, "accept", human_id=human_id,
        )

    def complete_task(
        self,
        task_id: str,
        summary: str = "",
        human_id: str = "human.user_001",
    ) -> CommandResult:
        """Human worker completes a task."""
        return self.submit_task_action(
            task_id, "complete", human_id=human_id, summary=summary,
        )

    def fail_task(
        self,
        task_id: str,
        reason: str = "",
        retryable: bool = False,
        human_id: str = "human.user_001",
    ) -> CommandResult:
        """Human worker fails a task."""
        return self.submit_task_action(
            task_id, "fail", human_id=human_id,
            reason=reason, retryable=retryable,
        )

    # -- Command router -----------------------------------------------------

    def execute(self, command: HumanCommand) -> CommandResult:
        """Execute a human command."""
        handlers = {
            "pause": lambda: self.pause(
                reason=command.params.get("reason", ""),
                human_id=command.human_id,
            ),
            "resume": lambda: self.resume(human_id=command.human_id),
            "send_email": lambda: self.send_email(
                to=command.params["to"],
                subject=command.params["subject"],
                body=command.params.get("body", ""),
                human_id=command.human_id,
                deliver_at_tick=command.params.get("deliver_at_tick"),
            ),
            "set_tick_duration": lambda: self.set_tick_duration(
                value=command.params["value"],
                unit=command.params.get("unit", "seconds"),
                effective_tick=command.params.get("effective_tick"),
                human_id=command.human_id,
            ),
            "accept_task": lambda: self.accept_task(
                task_id=command.params["task_id"],
                human_id=command.human_id,
            ),
            "complete_task": lambda: self.complete_task(
                task_id=command.params["task_id"],
                summary=command.params.get("summary", ""),
                human_id=command.human_id,
            ),
            "fail_task": lambda: self.fail_task(
                task_id=command.params["task_id"],
                reason=command.params.get("reason", ""),
                retryable=bool(command.params.get("retryable", False)),
                human_id=command.human_id,
            ),
            "view_status": lambda: CommandResult(
                success=True, command="view_status",
                data=self.view_simulation_status(),
            ),
            "view_agents": lambda: CommandResult(
                success=True, command="view_agents",
                data=self.view_agent_tree(),
            ),
            "view_tasks": lambda: CommandResult(
                success=True, command="view_tasks",
                data=self.view_task_tree(),
            ),
            "view_locks": lambda: CommandResult(
                success=True, command="view_locks",
                data=self.view_locks(),
            ),
        }

        handler = handlers.get(command.command)
        if handler is None:
            return CommandResult(
                success=False, command=command.command,
                message=f"Unknown command: {command.command}",
            )
        return handler()
