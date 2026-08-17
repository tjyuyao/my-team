"""Event-driven agent scheduler per SPEC §8.4, §9.2, §9.3.

Determines which agents activate each tick based on wake conditions
and pending events. Only agents with matching events are scheduled.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from my_team.agent_state import AgentState
from my_team.models.activation import (
    AgentActivation,
    ExecutionConfig,
    ReadyCandidate,
    WakeCondition,
    WakeupEvent,
)


class EventStatus(str, Enum):
    """Lifecycle status of a queued wake event."""

    QUEUED = "queued"
    ELIGIBLE = "eligible"
    CLAIMED = "claimed"
    CONSUMED = "consumed"
    DEFERRED = "deferred"
    EXPIRED = "expired"


class QueuedEvent(BaseModel):
    """A wake event with its processing status."""

    event: WakeupEvent = Field(description="The wake event")
    status: EventStatus = Field(
        default=EventStatus.QUEUED,
        description="Current processing status",
    )


def _matches(
    condition: WakeCondition,
    event: WakeupEvent,
    tick: int,
) -> bool:
    """Check if a wake event matches an agent's wake condition.

    Matching rules:
    - event.tick <= tick (not a future event)
    - tick >= condition.wake_at_tick (agent is eligible)
    - event.event_type in condition.event_types
    - condition.task_ids is empty OR event.task_id in condition.task_ids
    - condition.resources is empty OR event.resource in condition.resources
    - condition.thread_ids is empty OR event.thread_id in condition.thread_ids
    - condition.sender_ids is empty OR event.source_agent_id in condition.sender_ids
    """
    if event.tick > tick:
        return False
    if tick < condition.wake_at_tick:
        return False
    if event.event_type not in condition.event_types:
        return False
    if condition.task_ids and event.task_id and event.task_id not in condition.task_ids:
        return False
    if condition.resources and event.resource and event.resource not in condition.resources:
        return False
    if condition.thread_ids and event.thread_id and event.thread_id not in condition.thread_ids:
        return False
    if (
        condition.sender_ids
        and event.source_agent_id
        and event.source_agent_id not in condition.sender_ids
    ):
        return False
    return True


class AgentScheduler:
    """Event-driven scheduler that determines which agents activate each tick.

    Per SPEC §9.3, only agents meeting these conditions are scheduled:
    1. Agent has a pending wake event matching its WakeCondition
    2. wake_at_tick <= current_tick
    3. Agent state allows activation (not paused, not terminated)

    Multiple events for the same agent are merged into one ReadyCandidate.
    Max 1 activation per agent per tick.
    """

    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self._config = config or ExecutionConfig()
        self._wake_conditions: dict[str, WakeCondition] = {}
        self._events: list[QueuedEvent] = []
        self._activations_this_tick: dict[str, AgentActivation] = {}
        self._activation_history: list[AgentActivation] = []
        self._activation_counter = 0

    @property
    def config(self) -> ExecutionConfig:
        return self._config

    def register_agent(
        self,
        agent_id: str,
        initial_condition: WakeCondition | None = None,
    ) -> None:
        """Register an agent with the scheduler."""
        self._wake_conditions[agent_id] = initial_condition or WakeCondition()

    def update_wake_condition(
        self,
        agent_id: str,
        condition: WakeCondition,
    ) -> None:
        """Update an agent's wake condition."""
        self._wake_conditions[agent_id] = condition

    def get_wake_condition(self, agent_id: str) -> WakeCondition | None:
        """Get an agent's current wake condition."""
        return self._wake_conditions.get(agent_id)

    def enqueue_event(self, event: WakeupEvent) -> None:
        """Enqueue a wake event for processing.

        Events are only eligible for matching in subsequent calls to
        compute_ready_set() — not immediately.
        """
        self._events.append(QueuedEvent(event=event))

    def compute_ready_set(
        self,
        tick: int,
        agent_states: dict[str, AgentState],
    ) -> list[ReadyCandidate]:
        """Determine which agents should activate this tick.

        Returns list of ReadyCandidate in deterministic order (sorted by agent_id).
        Each candidate contains all matching events for that agent.
        """
        # Mark eligible events
        for qe in self._events:
            if qe.status == EventStatus.QUEUED:
                qe.status = EventStatus.ELIGIBLE

        # Group matching events by agent
        agent_events: dict[str, list[WakeupEvent]] = {}
        for qe in self._events:
            if qe.status != EventStatus.ELIGIBLE:
                continue
            event = qe.event
            agent_id = event.target_agent_id

            # Check if agent exists and is in a schedulable state
            state = agent_states.get(agent_id)
            if state is None:
                continue
            if state in (
                AgentState.PAUSED,
                AgentState.FAILED,
                AgentState.TERMINATED,
                AgentState.CREATED,
                AgentState.INITIALIZED,
            ):
                continue

            # Check wake condition
            condition = self._wake_conditions.get(agent_id)
            if condition is None:
                continue

            if _matches(condition, event, tick):
                if agent_id not in agent_events:
                    agent_events[agent_id] = []
                agent_events[agent_id].append(event)

        # Build ReadyCandidates (deterministic order)
        candidates: list[ReadyCandidate] = []
        for agent_id in sorted(agent_events.keys()):
            events = tuple(agent_events[agent_id])
            candidates.append(ReadyCandidate(
                agent_id=agent_id,
                events=events,
                tick=tick,
            ))

        return candidates

    def claim_events(
        self,
        agent_id: str,
        activation_id: str,
        event_ids: list[str],
    ) -> tuple[WakeupEvent, ...]:
        """Mark specific events as claimed for an activation.

        Returns the claimed events. If an activation fails later,
        call defer_events() to return them to eligible status.
        """
        claimed: list[WakeupEvent] = []
        for qe in self._events:
            if qe.status == EventStatus.ELIGIBLE and qe.event.event_id in event_ids:
                qe.status = EventStatus.CLAIMED
                claimed.append(qe.event)
        return tuple(claimed)

    def defer_events(self, event_ids: list[str]) -> None:
        """Return claimed events to eligible status (activation failed)."""
        for qe in self._events:
            if qe.status == EventStatus.CLAIMED and qe.event.event_id in event_ids:
                qe.status = EventStatus.ELIGIBLE

    def consume_events(self, event_ids: list[str]) -> None:
        """Mark events as consumed after successful activation."""
        for qe in self._events:
            if qe.status == EventStatus.CLAIMED and qe.event.event_id in event_ids:
                qe.status = EventStatus.CONSUMED

    def requeue_events(self, event_ids: list[str]) -> None:
        """Return claimed/eligible events to QUEUED (rollback recovery).

        After a tick ROLLBACK the state the activation observed was
        invalidated; the wake events must re-trigger activation next
        tick. QUEUED survives end_tick (which only expires ELIGIBLE
        events that were never claimed this tick).
        """
        for qe in self._events:
            if (
                qe.status in {EventStatus.CLAIMED, EventStatus.ELIGIBLE}
                and qe.event.event_id in event_ids
            ):
                qe.status = EventStatus.QUEUED

    def begin_activation(
        self,
        candidate: ReadyCandidate,
        tick: int,
    ) -> AgentActivation:
        """Record the start of an activation.

        Enforces max 1 activation per agent per tick.
        """
        if candidate.agent_id in self._activations_this_tick:
            raise ValueError(
                f"Agent '{candidate.agent_id}' already has an activation "
                f"in tick {tick}"
            )

        self._activation_counter += 1
        activation = AgentActivation(
            activation_id=f"act.{self._activation_counter:06d}",
            agent_id=candidate.agent_id,
            tick=tick,
            wake_events=candidate.events,
        )

        # Claim the events
        event_ids = [e.event_id for e in candidate.events]
        self.claim_events(candidate.agent_id, activation.activation_id, event_ids)

        self._activations_this_tick[candidate.agent_id] = activation
        return activation

    def complete_activation(
        self,
        activation_id: str,
        *,
        success: bool = True,
        error: str | None = None,
        llm_calls: int = 0,
        tool_calls: int = 0,
        llm_invocation_id: str | None = None,
    ) -> AgentActivation:
        """Mark an activation as completed."""
        activation = None
        for act in self._activations_this_tick.values():
            if act.activation_id == activation_id:
                activation = act
                break

        if activation is None:
            raise ValueError(f"Activation '{activation_id}' not found")

        activation.completed = success
        activation.error = error
        activation.llm_calls = llm_calls
        activation.tool_calls = tool_calls
        activation.llm_invocation_id = llm_invocation_id

        # Consume claimed events on success, defer on failure
        event_ids = [e.event_id for e in activation.wake_events]
        if success:
            self.consume_events(event_ids)
        else:
            self.defer_events(event_ids)

        self._activation_history.append(activation)
        return activation

    def end_tick(self) -> None:
        """Clean up at end of tick. Expires unconsumed eligible events."""
        for qe in self._events:
            if qe.status == EventStatus.ELIGIBLE:
                qe.status = EventStatus.EXPIRED
        self._activations_this_tick.clear()

    def get_activation_history(self) -> list[AgentActivation]:
        """Return all activation records for audit."""
        return list(self._activation_history)

    def pending_event_count(self) -> int:
        """Count events that are still eligible or claimed."""
        return sum(
            1 for qe in self._events
            if qe.status in (EventStatus.QUEUED, EventStatus.ELIGIBLE, EventStatus.CLAIMED)
        )

    def all_events(self) -> list[QueuedEvent]:
        """Return all queued events (for inspection/testing)."""
        return list(self._events)

    def clear(self) -> None:
        """Clear all events and activations (for testing)."""
        self._events.clear()
        self._activations_this_tick.clear()
        self._activation_history.clear()
        self._activation_counter = 0
