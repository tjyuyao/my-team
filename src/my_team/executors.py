"""Execution mode implementations: discrete async and bounded micro loop.

Per SPEC §8.5:
- DiscreteAsync: one LLM call per activation, tool results in next tick
- BoundedMicroLoop: limited LLM→Tool→LLM rounds within one activation
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from my_team.agent_runtime import (
    ActionContext,
    AgentRuntime,
    AgentSnapshot,
    ToolContext,
    ToolRegistry,
)
from my_team.agent_state import AgentState
from my_team.models.activation import AgentActivation, ExecutionConfig


class ToolInvocationRecord(BaseModel):
    """Record of a single tool call within an activation.

    Stores hashes and summaries, not raw arguments/results,
    for privacy and size control.
    """

    invocation_id: str = Field(description="Unique invocation ID")
    activation_id: str = Field(description="Activation this belongs to")
    agent_id: str = Field(description="Agent that made the call")
    tick: int = Field(description="Tick of the call")
    tool_name: str = Field(description="Tool that was called")
    arguments_hash: str = Field(default="", description="Hash of arguments (privacy)")
    result_summary: str = Field(default="", description="Summary of result (size control)")
    success: bool = Field(default=True, description="Whether the call succeeded")


class ActivationResult(BaseModel):
    """Result of executing an activation."""

    activation: AgentActivation = Field(description="The activation record")
    tool_invocations: list[ToolInvocationRecord] = Field(
        default_factory=list,
        description="Tool calls made during activation",
    )
    resulting_state: AgentState = Field(
        description="Agent state after activation",
    )
    emails_sent: list[str] = Field(
        default_factory=list,
        description="Email IDs sent during activation",
    )


class DiscreteAsyncExecutor:
    """Discrete async execution mode, per SPEC §8.5 Mode A.

    Each activation:
    - At most 1 LLM call (via observe → decide)
    - Tool requests become staged effects
    - Tool results arrive in the next tick
    - All shared state committed at activation end
    """

    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self._tool_registry = tool_registry

    def execute_activation(
        self,
        agent: AgentRuntime,
        snapshot: AgentSnapshot,
        activation: AgentActivation,
        config: ExecutionConfig,
    ) -> ActivationResult:
        """Execute one activation in discrete async mode.

        Runs observe → decide → execute actions → return.
        """
        tool_invocations: list[ToolInvocationRecord] = []

        # Phase 1: Observe
        observation = agent.observe(snapshot)

        # Phase 2: Decide (may trigger LLM call)
        plan = agent.decide(observation)

        # Phase 3: Act — execute actions
        context = ActionContext(
            agent_id=agent.agent_id,
            tick=activation.tick,
            tool_context=ToolContext(
                agent_id=agent.agent_id,
                tick=activation.tick,
            ),
        )
        results = agent.act(plan, context)

        # Record tool invocations
        for result in results:
            invocation = ToolInvocationRecord(
                invocation_id=f"ti.{uuid.uuid4().hex[:12]}",
                activation_id=activation.activation_id,
                agent_id=agent.agent_id,
                tick=activation.tick,
                tool_name=result.action.tool_name,
                success=result.success,
                result_summary=str(result.result_data)[:200] if result.result_data else "",
            )
            tool_invocations.append(invocation)

        # Determine resulting state
        if results:
            resulting_state = AgentState.PROCESSING
        else:
            resulting_state = AgentState.IDLE

        return ActivationResult(
            activation=activation,
            tool_invocations=tool_invocations,
            resulting_state=resulting_state,
        )


class BoundedMicroLoopExecutor:
    """Bounded micro loop execution mode, per SPEC §8.5 Mode B.

    Allows limited LLM → Tool → LLM rounds within a single activation.
    All shared state committed at activation end, not mid-loop.
    Tool results visible within micro-loop (local), not globally until commit.
    """

    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self._tool_registry = tool_registry

    def execute_activation(
        self,
        agent: AgentRuntime,
        snapshot: AgentSnapshot,
        activation: AgentActivation,
        config: ExecutionConfig,
    ) -> ActivationResult:
        """Execute one activation with bounded micro loops.

       最多 max_micro_loop_rounds 轮 LLM → Tool.
        """
        tool_invocations: list[ToolInvocationRecord] = []
        max_rounds = config.max_micro_loop_rounds
        max_tool_calls = config.max_tool_calls_per_activation

        observation = agent.observe(snapshot)
        total_tool_calls = 0

        for round_num in range(max_rounds):
            # Decide
            plan = agent.decide(observation)

            if not plan.actions:
                break

            # Act
            context = ActionContext(
                agent_id=agent.agent_id,
                tick=activation.tick,
                tool_context=ToolContext(
                    agent_id=agent.agent_id,
                    tick=activation.tick,
                ),
            )
            results = agent.act(plan, context)

            for result in results:
                total_tool_calls += 1
                invocation = ToolInvocationRecord(
                    invocation_id=f"ti.{uuid.uuid4().hex[:12]}",
                    activation_id=activation.activation_id,
                    agent_id=agent.agent_id,
                    tick=activation.tick,
                    tool_name=result.action.tool_name,
                    success=result.success,
                    result_summary=str(result.result_data)[:200] if result.result_data else "",
                )
                tool_invocations.append(invocation)

            # Check budget
            if total_tool_calls >= max_tool_calls:
                break

        resulting_state = AgentState.PROCESSING if tool_invocations else AgentState.IDLE

        return ActivationResult(
            activation=activation,
            tool_invocations=tool_invocations,
            resulting_state=resulting_state,
        )
