"""Agent lifecycle state machine.

Per SPEC §9, manages agent states and transitions:
  created → initialized → ready → running → terminated

Running sub-states: idle, processing, waiting, blocked, paused, failed

All state transitions are validated against a transition table and
logged to an audit trail.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentState(str, Enum):
    """Agent lifecycle states per SPEC §9."""

    CREATED = "created"
    INITIALIZED = "initialized"
    READY = "ready"
    # Running sub-states
    IDLE = "idle"
    PROCESSING = "processing"
    WAITING = "waiting"  # kept for backward compat; prefer granular WAITING_FOR_*
    # Granular waiting sub-states per SPEC §9.1
    WAITING_FOR_LLM = "waiting_for_llm"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_CHILD = "waiting_for_child"
    WAITING_FOR_MAIL = "waiting_for_mail"
    WAITING_FOR_LOCK = "waiting_for_lock"
    WAITING_FOR_HUMAN = "waiting_for_human"
    BLOCKED = "blocked"
    PAUSED = "paused"
    FAILED = "failed"
    TERMINATED = "terminated"


# All granular waiting states
WAITING_STATES = {
    AgentState.WAITING,
    AgentState.WAITING_FOR_LLM,
    AgentState.WAITING_FOR_TOOL,
    AgentState.WAITING_FOR_CHILD,
    AgentState.WAITING_FOR_MAIL,
    AgentState.WAITING_FOR_LOCK,
    AgentState.WAITING_FOR_HUMAN,
}

# Top-level categories for grouping
PHASE_LIFECYCLE = {AgentState.CREATED, AgentState.INITIALIZED, AgentState.READY}
PHASE_RUNNING = {
    AgentState.IDLE, AgentState.PROCESSING,
    *WAITING_STATES,
    AgentState.BLOCKED, AgentState.PAUSED, AgentState.FAILED,
}
PHASE_TERMINAL = {AgentState.TERMINATED}


def is_running(state: AgentState) -> bool:
    """Check if a state is a running sub-state."""
    return state in PHASE_RUNNING


def is_terminal(state: AgentState) -> bool:
    """Check if a state is terminal (no further transitions expected)."""
    return state in PHASE_TERMINAL


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, agent_id: str, from_state: AgentState, to_state: AgentState) -> None:
        self.agent_id = agent_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Agent '{agent_id}': invalid transition {from_state.value} → {to_state.value}"
        )


# ---------------------------------------------------------------------------
# Transition table: allowed transitions from each state
# ---------------------------------------------------------------------------

# Key: source state, Value: set of allowed target states
TRANSITION_TABLE: dict[AgentState, set[AgentState]] = {
    # Lifecycle progression
    AgentState.CREATED: {AgentState.INITIALIZED},
    AgentState.INITIALIZED: {AgentState.READY},
    AgentState.READY: {AgentState.IDLE, AgentState.PROCESSING, AgentState.TERMINATED},
    # Running sub-states
    AgentState.IDLE: {
        AgentState.READY,        # wake event matched (event-driven scheduling)
        AgentState.PROCESSING,   # direct activation (legacy path)
        AgentState.PAUSED,       # system pause
        AgentState.FAILED,       # execution error
        AgentState.TERMINATED,   # normal termination
    },
    AgentState.PROCESSING: {
        AgentState.WAITING,              # generic wait (legacy)
        AgentState.WAITING_FOR_LLM,      # LLM call in flight
        AgentState.WAITING_FOR_TOOL,     # tool call in flight
        AgentState.WAITING_FOR_CHILD,    # waiting for child task result
        AgentState.WAITING_FOR_MAIL,     # waiting for specific email
        AgentState.WAITING_FOR_LOCK,     # waiting for shared KB lock
        AgentState.WAITING_FOR_HUMAN,    # waiting for human decision
        AgentState.BLOCKED,              # cannot self-resolve
        AgentState.IDLE,                 # done, back to idle
        AgentState.FAILED,               # execution error
        AgentState.PAUSED,               # system pause
    },
    # Generic waiting (legacy)
    AgentState.WAITING: {
        AgentState.PROCESSING,   # response received
        AgentState.BLOCKED,      # timeout or resource exhausted
        AgentState.FAILED,       # execution error
        AgentState.PAUSED,       # system pause
    },
    # Granular waiting sub-states
    AgentState.WAITING_FOR_LLM: {
        AgentState.PROCESSING,   # LLM result arrived
        AgentState.BLOCKED,      # LLM call failed permanently
        AgentState.FAILED,       # execution error
        AgentState.PAUSED,       # system pause
    },
    AgentState.WAITING_FOR_TOOL: {
        AgentState.PROCESSING,   # tool result arrived
        AgentState.BLOCKED,      # tool call failed permanently
        AgentState.FAILED,       # execution error
        AgentState.PAUSED,       # system pause
    },
    AgentState.WAITING_FOR_CHILD: {
        AgentState.PROCESSING,   # child task completed
        AgentState.BLOCKED,      # child timed out or failed
        AgentState.FAILED,       # execution error
        AgentState.PAUSED,       # system pause
    },
    AgentState.WAITING_FOR_MAIL: {
        AgentState.PROCESSING,   # email arrived
        AgentState.BLOCKED,      # mail timeout
        AgentState.FAILED,       # execution error
        AgentState.PAUSED,       # system pause
    },
    AgentState.WAITING_FOR_LOCK: {
        AgentState.PROCESSING,   # lock acquired
        AgentState.BLOCKED,      # lock unavailable
        AgentState.FAILED,       # execution error
        AgentState.PAUSED,       # system pause
    },
    AgentState.WAITING_FOR_HUMAN: {
        AgentState.PROCESSING,   # human response arrived
        AgentState.BLOCKED,      # human timeout
        AgentState.FAILED,       # execution error
        AgentState.PAUSED,       # system pause
    },
    AgentState.BLOCKED: {
        AgentState.IDLE,         # resolved by上级
        AgentState.PROCESSING,   # blockage resolved
        AgentState.FAILED,       # unrecoverable
        AgentState.TERMINATED,   # give up
    },
    AgentState.PAUSED: {
        AgentState.IDLE,         # resume to idle
        AgentState.PROCESSING,   # resume to processing
        AgentState.READY,        # resume to ready (scheduler will re-evaluate)
        AgentState.WAITING,      # resume to waiting
        AgentState.WAITING_FOR_LLM,
        AgentState.WAITING_FOR_TOOL,
        AgentState.WAITING_FOR_CHILD,
        AgentState.WAITING_FOR_MAIL,
        AgentState.WAITING_FOR_LOCK,
        AgentState.WAITING_FOR_HUMAN,
        AgentState.BLOCKED,      # resume to blocked
        AgentState.FAILED,       # resume then discover failure
        AgentState.TERMINATED,   # resume then terminate
    },
    AgentState.FAILED: {
        AgentState.IDLE,         # retry succeeded
        AgentState.TERMINATED,   # retry exhausted
        AgentState.PROCESSING,   # direct retry
    },
    # Terminal state: no transitions out
    AgentState.TERMINATED: set(),
}


class AuditEntry(BaseModel):
    """A single audit log entry for a state transition."""

    timestamp: str = Field(description="ISO-8601 timestamp")
    agent_id: str = Field(description="Agent that transitioned")
    from_state: AgentState = Field(description="Previous state")
    to_state: AgentState = Field(description="New state")
    tick: int | None = Field(default=None, description="Simulation tick at transition")
    reason: str = Field(default="", description="Optional reason for the transition")
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditLog:
    """Append-only audit log for state transitions."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(
        self,
        agent_id: str,
        from_state: AgentState,
        to_state: AgentState,
        tick: int | None = None,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Record a state transition."""
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            from_state=from_state,
            to_state=to_state,
            tick=tick,
            reason=reason,
            metadata=metadata or {},
        )
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> list[AuditEntry]:
        """All audit entries (read-only copy)."""
        return list(self._entries)

    def for_agent(self, agent_id: str) -> list[AuditEntry]:
        """Get all entries for a specific agent."""
        return [e for e in self._entries if e.agent_id == agent_id]

    def last_for_agent(self, agent_id: str) -> AuditEntry | None:
        """Get the most recent entry for an agent."""
        agent_entries = self.for_agent(agent_id)
        return agent_entries[-1] if agent_entries else None

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"AuditLog({len(self._entries)} entries)"


class AgentStateMachine:
    """Manages the lifecycle state of a single agent.

    Validates transitions against the transition table and records
    all changes to the audit log.
    """

    def __init__(
        self,
        agent_id: str,
        initial_state: AgentState = AgentState.CREATED,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._state = initial_state
        self._audit_log = audit_log if audit_log is not None else AuditLog()
        self._transition_count = 0

        # Record initial state
        if initial_state != AgentState.CREATED:
            self._audit_log.record(
                agent_id=agent_id,
                from_state=AgentState.CREATED,
                to_state=initial_state,
                reason="initialized with non-default state",
            )

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def transition_count(self) -> int:
        return self._transition_count

    @property
    def is_running(self) -> bool:
        return is_running(self._state)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self._state)

    def can_transition_to(self, target: AgentState) -> bool:
        """Check if a transition to the target state is allowed."""
        allowed = TRANSITION_TABLE.get(self._state, set())
        return target in allowed

    def transition(
        self,
        target: AgentState,
        tick: int | None = None,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Execute a state transition.

        Args:
            target: The desired target state.
            tick: Optional simulation tick at transition.
            reason: Optional reason for audit.
            metadata: Optional extra data for audit.

        Returns:
            The audit entry for this transition.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
        """
        if not self.can_transition_to(target):
            raise InvalidTransitionError(self._agent_id, self._state, target)

        old_state = self._state
        self._state = target
        self._transition_count += 1

        entry = self._audit_log.record(
            agent_id=self._agent_id,
            from_state=old_state,
            to_state=target,
            tick=tick,
            reason=reason,
            metadata=metadata,
        )
        return entry

    # -- Convenience transition methods --------------------------------------

    def initialize(self, **kwargs: Any) -> AuditEntry:
        """created → initialized"""
        return self.transition(AgentState.INITIALIZED, **kwargs)

    def mark_ready(self, **kwargs: Any) -> AuditEntry:
        """initialized → ready"""
        return self.transition(AgentState.READY, **kwargs)

    def start(self, **kwargs: Any) -> AuditEntry:
        """ready → idle (agent begins running)"""
        return self.transition(AgentState.IDLE, **kwargs)

    def begin_processing(self, **kwargs: Any) -> AuditEntry:
        """idle → processing"""
        return self.transition(AgentState.PROCESSING, **kwargs)

    def wait(self, **kwargs: Any) -> AuditEntry:
        """processing → waiting"""
        return self.transition(AgentState.WAITING, **kwargs)

    def wait_for_llm(self, **kwargs: Any) -> AuditEntry:
        """processing → waiting_for_llm"""
        return self.transition(AgentState.WAITING_FOR_LLM, **kwargs)

    def wait_for_tool(self, **kwargs: Any) -> AuditEntry:
        """processing → waiting_for_tool"""
        return self.transition(AgentState.WAITING_FOR_TOOL, **kwargs)

    def wait_for_child(self, **kwargs: Any) -> AuditEntry:
        """processing → waiting_for_child"""
        return self.transition(AgentState.WAITING_FOR_CHILD, **kwargs)

    def wait_for_mail(self, **kwargs: Any) -> AuditEntry:
        """processing → waiting_for_mail"""
        return self.transition(AgentState.WAITING_FOR_MAIL, **kwargs)

    def wait_for_lock(self, **kwargs: Any) -> AuditEntry:
        """processing → waiting_for_lock"""
        return self.transition(AgentState.WAITING_FOR_LOCK, **kwargs)

    def wait_for_human(self, **kwargs: Any) -> AuditEntry:
        """processing → waiting_for_human"""
        return self.transition(AgentState.WAITING_FOR_HUMAN, **kwargs)

    def wake_up(self, **kwargs: Any) -> AuditEntry:
        """idle → ready (wake event matched)"""
        return self.transition(AgentState.READY, **kwargs)

    def block(self, **kwargs: Any) -> AuditEntry:
        """processing/waiting → blocked"""
        return self.transition(AgentState.BLOCKED, **kwargs)

    def resume_from_wait(self, **kwargs: Any) -> AuditEntry:
        """waiting → processing"""
        return self.transition(AgentState.PROCESSING, **kwargs)

    def resolve_block(self, **kwargs: Any) -> AuditEntry:
        """blocked → idle"""
        return self.transition(AgentState.IDLE, **kwargs)

    def finish_processing(self, **kwargs: Any) -> AuditEntry:
        """processing → idle"""
        return self.transition(AgentState.IDLE, **kwargs)

    def pause(self, **kwargs: Any) -> AuditEntry:
        """any running state → paused"""
        return self.transition(AgentState.PAUSED, **kwargs)

    def unpause(self, target: AgentState = AgentState.IDLE, **kwargs: Any) -> AuditEntry:
        """paused → target state"""
        return self.transition(target, **kwargs)

    def fail(self, **kwargs: Any) -> AuditEntry:
        """any running state → failed"""
        return self.transition(AgentState.FAILED, **kwargs)

    def recover(self, **kwargs: Any) -> AuditEntry:
        """failed → idle (retry succeeded)"""
        return self.transition(AgentState.IDLE, **kwargs)

    def terminate(self, **kwargs: Any) -> AuditEntry:
        """→ terminated"""
        return self.transition(AgentState.TERMINATED, **kwargs)

    def __repr__(self) -> str:
        return f"AgentStateMachine({self._agent_id}, state={self._state.value})"
