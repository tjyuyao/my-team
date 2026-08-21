"""Agent activation and scheduling models per SPEC §8.4, §9.1, §9.2.

Defines the event-driven activation system: wake conditions, wakeup events,
activation records, and execution configuration.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WakeEventType(str, Enum):
    """Types of events that can wake an agent, per SPEC §9.2."""

    NEW_EMAIL = "new_email"
    TOOL_RESULT = "tool_result"
    CHILD_TASK_CHANGE = "child_task_change"
    LOCK_AVAILABLE = "lock_available"
    RETRY_TIMER = "retry_timer"
    HUMAN_MESSAGE = "human_message"
    DEADLINE_APPROACHING = "deadline_approaching"
    TIMER_EXPIRY = "timer_expiry"
    BOOTSTRAP = "bootstrap"
    EXTERNAL_RESULT = "external_result"  # T9: outbound external op completed


class WaitingState(str, Enum):
    """Granular waiting sub-states, per SPEC §9.1."""

    WAITING_FOR_LLM = "waiting_for_llm"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_CHILD = "waiting_for_child"
    WAITING_FOR_MAIL = "waiting_for_mail"
    WAITING_FOR_LOCK = "waiting_for_lock"
    WAITING_FOR_HUMAN = "waiting_for_human"
    WAITING_FOR_EXTERNAL = "waiting_for_external"  # T9: awaiting outbound op


class WakeCondition(BaseModel):
    """Per-agent wake condition, per SPEC §9.2.

    The scheduler checks these conditions each tick to determine
    which agents to activate. Empty set fields mean 'no restriction'
    (match all).
    """

    event_types: set[WakeEventType] = Field(
        default_factory=lambda: {WakeEventType.BOOTSTRAP},
        description="Event types that can wake this agent",
    )
    wake_at_tick: int = Field(
        default=0,
        ge=0,
        description="Earliest tick at which this agent can be woken",
    )
    task_ids: set[str] = Field(
        default_factory=set,
        description="Associated task IDs (empty = match all tasks)",
    )
    resources: set[str] = Field(
        default_factory=set,
        description="Associated shared KB resources (empty = match all)",
    )
    thread_ids: set[str] = Field(
        default_factory=set,
        description="Associated email thread IDs (empty = match all)",
    )
    sender_ids: set[str] = Field(
        default_factory=set,
        description="Expected sender agent IDs (empty = match all senders)",
    )


class WakeupEvent(BaseModel):
    """An event that may wake an agent, per SPEC §9.2.

    Events produced in tick t are only visible in tick t+1's
    Deliver/Schedule phase.  ``visible_at_tick`` makes this explicit
    (default: tick + 1) — ``_matches`` uses it instead of relying on
    ordering side-effects.
    """

    event_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex[:12],
        description="Unique event identifier",
    )
    event_type: WakeEventType = Field(description="Type of wake event")
    target_agent_id: str = Field(description="Agent to be woken")
    tick: int = Field(description="Tick when event was produced")
    visible_at_tick: int = Field(
        default=-1,
        description=(
            "Tick from which this event is eligible for matching. "
            "Defaults to tick+1 (set at enqueue time). -1 means unset."
        ),
    )
    source_agent_id: str = Field(
        default="",
        description="Agent that produced this event",
    )
    task_id: str = Field(default="", description="Related task ID")
    resource: str = Field(default="", description="Related shared KB resource")
    thread_id: str = Field(default="", description="Related email thread ID")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional event details",
    )


class ReadyCandidate(BaseModel):
    """Agent ready for activation with its triggering events.

    Multiple wake events for the same agent are merged into one
    ReadyCandidate.
    """

    agent_id: str = Field(description="Agent ready for activation")
    events: tuple[WakeupEvent, ...] = Field(
        description="All matching wake events for this agent",
    )
    tick: int = Field(description="Current tick")


class AgentActivation(BaseModel):
    """Records a single activation of an agent within a tick, per SPEC §8.4.

    An activation = Observe → Decide → Actions → Commit.
    Max 1 activation per agent per tick.
    """

    activation_id: str = Field(
        default_factory=lambda: f"act.{uuid.uuid4().hex[:12]}",
        description="Unique activation identifier",
    )
    agent_id: str = Field(description="Agent being activated")
    tick: int = Field(description="Tick in which activation occurs")
    wake_events: tuple[WakeupEvent, ...] = Field(
        default_factory=tuple,
        description="Events that triggered this activation",
    )
    completed: bool = Field(
        default=False,
        description="Whether activation completed successfully",
    )
    llm_calls: int = Field(
        default=0,
        description="Number of LLM calls in this activation",
    )
    tool_calls: int = Field(
        default=0,
        description="Number of tool calls in this activation",
    )
    llm_invocation_id: str | None = Field(
        default=None,
        description="ID of the LLM invocation if any",
    )
    error: str | None = Field(
        default=None,
        description="Error message if activation failed",
    )


class ExecutionConfig(BaseModel):
    """Per-activation budget limits (SPEC §3.1「每 tick 一轮」).

    There is no "execution mode" choice: each agent activates at most
    once per tick with at most one Decide (one LLM call); multi-step
    reasoning is a cross-tick ReAct continuation. A tick is the atomic
    state-commit unit and therefore aligns with one round of ReAct tool
    calls by construction — the former BOUNDED_MICRO_LOOP mode (legacy
    SPEC §8.5) was never wired and is formally abolished. These fields
    are hard budget caps, not modes.
    """

    max_llm_calls_per_activation: int = Field(
        default=1,
        ge=0,
        le=16,
        description="Maximum LLM calls per activation",
    )
    max_tool_calls_per_activation: int = Field(
        default=8,
        ge=0,
        le=128,
        description="Maximum tool calls per activation",
    )
    max_action_budget: int = Field(
        default=32,
        ge=1,
        le=1024,
        description="Maximum total actions per activation",
    )
    max_active_agents_per_tick: int = Field(
        default=8,
        ge=1,
        le=4096,
        description=(
            "Activation capacity per tick (SPEC §14.1 抗超负荷): Schedule "
            "selects within capacity by (priority, deadline) — real time, "
            "direct comparison; over-capacity agents stay ready and "
            "re-compete next tick (idempotent, no state loss). T11 决策 2."
        ),
    )


class AgentWaitState(BaseModel):
    """Persistent wait state for an agent.

    Saved as Agent Runtime State so it survives pause/resume and
    can be inspected by the scheduler.
    """

    waiting_state: WaitingState = Field(description="Which waiting sub-state")
    condition: WakeCondition = Field(description="Wake condition for this wait")
    entered_at_tick: int = Field(description="Tick when agent entered this state")
    task_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Task IDs being waited on",
    )
    call_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Tool/LLM call IDs being waited on",
    )
