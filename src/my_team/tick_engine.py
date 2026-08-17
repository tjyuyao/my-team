"""Discrete time step simulation engine.

Per SPEC §8:
- Discrete ticks: 0, 1, 2, ...
- 7 phases per tick: Freeze, Deliver, Observe, Decide, Act, Commit, Audit
- Configurable tick duration and simulation-time-per-tick
- Read consistency via snapshots: all agents see the same state within a tick
- Actions committed atomically at tick boundary
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field


class TickPhase(str, Enum):
    """The 7 phases of a single tick, per SPEC §8.2."""

    FREEZE = "freeze"      # Snapshot global state
    DELIVER = "deliver"    # Deliver queued emails
    OBSERVE = "observe"    # Agents read inputs
    DECIDE = "decide"      # Agents plan actions
    ACT = "act"            # Agents execute in parallel
    COMMIT = "commit"      # Atomic state update
    AUDIT = "audit"        # Record audit events


class TickConfig(BaseModel):
    """Configuration for tick behavior, per SPEC §8.1."""

    tick_duration_value: int = Field(
        default=10,
        description="Duration of one tick in real-time units",
    )
    tick_duration_unit: str = Field(
        default="seconds",
        description="Unit: seconds, minutes, hours",
    )
    simulation_time_per_tick_value: int = Field(
        default=1,
        description="How much simulation time one tick represents",
    )
    simulation_time_per_tick_unit: str = Field(
        default="hour",
        description="Unit: minute, hour, day",
    )
    start_paused: bool = Field(
        default=False,
        description="Whether simulation starts in paused state",
    )
    deterministic_mode: bool = Field(
        default=True,
        description="Ensure reproducible execution order",
    )


class SimulationState(str, Enum):
    """Top-level simulation status."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


class TickSnapshot(BaseModel):
    """A frozen snapshot of the system state at the start of a tick.

    All agents read from this snapshot during OBSERVE and DECIDE phases,
    ensuring read consistency (SPEC §13.1).
    """

    tick: int = Field(description="The tick number this snapshot represents")
    agents: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Agent states at snapshot time",
    )
    emails: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Pending emails at snapshot time",
    )
    shared_kb: dict[str, Any] = Field(
        default_factory=dict,
        description="Shared knowledge base state",
    )
    locks: dict[str, Any] = Field(
        default_factory=dict,
        description="Active lock states",
    )
    tasks: dict[str, Any] = Field(
        default_factory=dict,
        description="Task states",
    )


class TickResult(BaseModel):
    """Result of executing a single tick."""

    tick: int = Field(description="Tick number that was executed")
    phases_completed: list[TickPhase] = Field(description="Phases that ran")
    agent_actions: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict,
        description="Actions produced by each agent",
    )
    emails_queued: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Emails queued during this tick",
    )
    committed: bool = Field(default=True, description="Whether actions were committed")
    errors: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Errors encountered during the tick",
    )
    audit_events: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Audit events generated during the tick",
    )


class AgentAction(BaseModel):
    """An action produced by an agent during the DECIDE/ACT phases."""

    agent_id: str
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    tick: int = 0


# Type alias for phase handlers
PhaseHandler = Callable[[int, TickSnapshot | None, dict[str, Any]], dict[str, Any]]


class TickEngine:
    """Core simulation engine that drives discrete time steps.

    Manages the tick clock, executes the 7-phase cycle, and coordinates
    agent execution with read consistency guarantees.
    """

    def __init__(self, config: TickConfig | None = None) -> None:
        self._config = config or TickConfig()
        self._current_tick = 0
        self._state = (
            SimulationState.PAUSED if self._config.start_paused
            else SimulationState.CREATED
        )
        self._tick_history: list[TickResult] = []
        self._snapshots: dict[int, TickSnapshot] = {}

        # Phase handlers: can be overridden or extended
        self._phase_handlers: dict[TickPhase, PhaseHandler] = {
            TickPhase.FREEZE: self._phase_freeze,
            TickPhase.DELIVER: self._phase_deliver,
            TickPhase.OBSERVE: self._phase_observe,
            TickPhase.DECIDE: self._phase_decide,
            TickPhase.ACT: self._phase_act,
            TickPhase.COMMIT: self._phase_commit,
            TickPhase.AUDIT: self._phase_audit,
        }

        # Shared context that phases read/write
        self._context: dict[str, Any] = {
            "pending_emails": [],
            "agent_snapshots": {},
            "agent_actions": {},
            "committed_actions": [],
            "audit_events": [],
        }

    @property
    def current_tick(self) -> int:
        return self._current_tick

    @property
    def state(self) -> SimulationState:
        return self._state

    @property
    def config(self) -> TickConfig:
        return self._config

    def get_snapshot(self, tick: int | None = None) -> TickSnapshot | None:
        """Get the snapshot for a specific tick.

        If tick is None, returns the most recent snapshot.
        """
        if tick is not None:
            return self._snapshots.get(tick)
        if not self._snapshots:
            return None
        latest_tick = max(self._snapshots.keys())
        return self._snapshots[latest_tick]

    def can_advance(self) -> bool:
        """Check if the simulation can advance to the next tick."""
        return self._state in (SimulationState.CREATED, SimulationState.RUNNING)

    def pause(self) -> None:
        """Pause the simulation. Takes effect after current tick (SPEC §12.1)."""
        if self._state == SimulationState.RUNNING:
            self._state = SimulationState.PAUSED

    def resume(self) -> None:
        """Resume from paused state (SPEC §12.2)."""
        if self._state == SimulationState.PAUSED:
            self._state = SimulationState.RUNNING

    def advance(self, count: int = 1) -> list[TickResult]:
        """Advance the simulation by one or more ticks.

        Each tick runs through all 7 phases in order.
        All agents execute within each tick with read consistency.

        Args:
            count: Number of ticks to advance.

        Returns:
            List of TickResult for each tick executed.
        """
        if not self.can_advance():
            raise RuntimeError(
                f"Cannot advance: simulation is {self._state.value}"
            )

        # Transition to running on first advance
        if self._state == SimulationState.CREATED:
            self._state = SimulationState.RUNNING

        results: list[TickResult] = []
        for _ in range(count):
            result = self._execute_tick()
            results.append(result)
            self._tick_history.append(result)
            self._current_tick += 1

        return results

    def _execute_tick(self) -> TickResult:
        """Execute a single tick through all 7 phases."""
        tick = self._current_tick
        phases_completed: list[TickPhase] = []
        errors: list[dict[str, Any]] = []
        audit_events: list[dict[str, Any]] = []
        agent_actions: dict[str, list[dict[str, Any]]] = {}
        emails_queued: list[dict[str, Any]] = []

        # Reset context for this tick
        self._context["agent_actions"] = {}
        self._context["committed_actions"] = []
        self._context["audit_events"] = []

        for phase in TickPhase:
            try:
                result = self._phase_handlers[phase](tick, self._snapshots.get(tick), self._context)
                phases_completed.append(phase)

                # Collect results from phase
                if "agent_actions" in result:
                    agent_actions.update(result["agent_actions"])
                if "emails_queued" in result:
                    emails_queued.extend(result["emails_queued"])
                if "audit_events" in result:
                    audit_events.extend(result["audit_events"])

            except Exception as e:
                errors.append({
                    "tick": tick,
                    "phase": phase.value,
                    "error": str(e),
                    "type": type(e).__name__,
                })
                # On error, skip remaining phases for this tick
                break

        return TickResult(
            tick=tick,
            phases_completed=phases_completed,
            agent_actions=agent_actions,
            emails_queued=emails_queued,
            committed=TickPhase.COMMIT in phases_completed,
            errors=errors,
            audit_events=audit_events,
        )

    # -- Phase handlers -----------------------------------------------------

    def _phase_freeze(
        self, tick: int, snapshot: TickSnapshot | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 1: Freeze - snapshot the current global state.

        All agents will read from this snapshot during OBSERVE.
        """
        new_snapshot = TickSnapshot(
            tick=tick,
            agents=dict(context.get("agent_snapshots", {})),
            emails=list(context.get("pending_emails", [])),
            shared_kb=dict(context.get("shared_kb", {})),
            locks=dict(context.get("locks", {})),
            tasks=dict(context.get("tasks", {})),
        )
        self._snapshots[tick] = new_snapshot
        return {"snapshot": new_snapshot}

    def _phase_deliver(
        self, tick: int, snapshot: TickSnapshot | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 2: Deliver - deliver emails whose deliver_at_tick <= current_tick.

        Emails enter target agents' inboxes.
        """
        if snapshot is None:
            return {}

        delivered = []
        remaining = []
        for email in snapshot.emails:
            deliver_at = email.get("deliver_at_tick", 0)
            if deliver_at <= tick:
                delivered.append(email)
            else:
                remaining.append(email)

        context["pending_emails"] = remaining
        return {"emails_queued": delivered}

    def _phase_observe(
        self, tick: int, snapshot: TickSnapshot | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 3: Observe - agents read from the frozen snapshot.

        Each agent reads: new emails, task state, private workspace,
        memory, shared KB, lock status, system notifications.
        """
        if snapshot is None:
            return {}

        # Create per-agent observation data from the snapshot
        observations: dict[str, dict[str, Any]] = {}
        for agent_id, agent_state in snapshot.agents.items():
            observations[agent_id] = {
                "tick": tick,
                "agent_id": agent_id,
                "state": agent_state,
                "emails": [
                    e for e in snapshot.emails
                    if agent_id in e.get("to", [])
                ],
                "shared_kb": snapshot.shared_kb,
                "locks": snapshot.locks,
            }

        context["observations"] = observations
        return {}

    def _phase_decide(
        self, tick: int, snapshot: TickSnapshot | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 4: Decide - agents independently generate action plans.

        Each agent produces a list of planned actions based on its observations.
        In a real system, this is where LLM inference happens.
        """
        # Default: no actions (agents are inert until given behavior)
        # Subclasses or callbacks can override this
        context["agent_actions"] = {}
        return {"agent_actions": {}}

    def _phase_act(
        self, tick: int, snapshot: TickSnapshot | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 5: Act - agents execute their planned actions in parallel.

        Execution results are temporary and not visible to other agents
        until COMMIT.
        """
        actions = context.get("agent_actions", {})
        # In default implementation, actions are just recorded
        # Real execution happens in subclasses
        context["committed_actions"] = actions
        return {"agent_actions": actions}

    def _phase_commit(
        self, tick: int, snapshot: TickSnapshot | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 6: Commit - atomic state update.

        Applies all agent actions in deterministic order:
        1. State transitions
        2. Email queueing
        3. Private space writes
        4. Lock acquire/release
        5. Shared KB updates
        6. Memory persistence
        7. Task state updates
        """
        committed = context.get("committed_actions", {})
        emails_queued: list[dict[str, Any]] = []

        for agent_id, action_list in committed.items():
            if not isinstance(action_list, list):
                continue
            for action in action_list:
                if action.get("action_type") == "send_email":
                    emails_queued.append(action.get("payload", {}))

        # Update pending emails
        context["pending_emails"].extend(emails_queued)

        return {"emails_queued": emails_queued}

    def _phase_audit(
        self, tick: int, snapshot: TickSnapshot | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase 7: Audit - record all events for this tick.

        Logs: agent inputs, actions, tool calls, emails, task changes,
        KB versions, lock events, errors, human operations.
        """
        events = [
            {
                "tick": tick,
                "event_type": "tick_complete",
                "phases_executed": [p.value for p in TickPhase],
            }
        ]
        context["audit_events"] = events
        return {"audit_events": events}

    def register_phase_handler(self, phase: TickPhase, handler: PhaseHandler) -> None:
        """Override or extend a phase handler."""
        self._phase_handlers[phase] = handler

    def update_context(self, **kwargs: Any) -> None:
        """Update the shared context (e.g., inject agent states or emails)."""
        self._context.update(kwargs)

    def get_context(self) -> dict[str, Any]:
        """Get the current shared context."""
        return dict(self._context)

    @property
    def history(self) -> list[TickResult]:
        """History of all executed ticks."""
        return list(self._tick_history)

    def __repr__(self) -> str:
        return (
            f"TickEngine(tick={self._current_tick}, "
            f"state={self._state.value})"
        )
