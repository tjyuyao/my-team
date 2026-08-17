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
    phases_completed: list[str] = Field(description="Phases that ran")
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
    """Discrete-time clock for the simulation kernel.

    Manages tick counter and CREATED/RUNNING/PAUSED/COMPLETED state.
    Phase execution lives in Simulation.run_tick() — this class is
    intentionally a pure clock with no phase logic.
    """

    def __init__(self, config: TickConfig | None = None) -> None:
        self._config = config or TickConfig()
        self._current_tick = 0
        self._state = (
            SimulationState.PAUSED if self._config.start_paused
            else SimulationState.CREATED
        )

    @property
    def current_tick(self) -> int:
        return self._current_tick

    @property
    def state(self) -> SimulationState:
        return self._state

    @property
    def config(self) -> TickConfig:
        return self._config

    def can_advance(self) -> bool:
        """Check if the simulation can advance to the next tick."""
        return self._state in (SimulationState.CREATED, SimulationState.RUNNING)

    def pause(self) -> None:
        """Pause the simulation. Takes effect after current tick (SPEC §12.1).

        Can pause from CREATED (before first tick) or RUNNING.
        """
        if self._state in (SimulationState.RUNNING, SimulationState.CREATED):
            self._state = SimulationState.PAUSED

    def resume(self) -> None:
        """Resume from paused state (SPEC §12.2)."""
        if self._state == SimulationState.PAUSED:
            self._state = SimulationState.RUNNING

    def advance(self, count: int = 1) -> None:
        """Advance the clock by one or more ticks.

        Does NOT execute any phase logic — Simulation.run_tick() owns
        the 10-phase kernel cycle.
        """
        if not self.can_advance():
            raise RuntimeError(
                f"Cannot advance: simulation is {self._state.value}"
            )
        if self._state == SimulationState.CREATED:
            self._state = SimulationState.RUNNING
        self._current_tick += count

    def __repr__(self) -> str:
        return (
            f"TickEngine(tick={self._current_tick}, "
            f"state={self._state.value})"
        )
