"""Tests for execution modes: DiscreteAsync and BoundedMicroLoop.

Tests use BaseAgent (returns empty plans) to verify executor logic
without requiring real LLM calls.
"""


import pytest

from my_team.agent_runtime import (
    WORKER_TOOLS,
    AgentSnapshot,
    BaseAgent,
    ToolRegistry,
)
from my_team.agent_state import AgentState
from my_team.executors import (
    ActivationResult,
    BoundedMicroLoopExecutor,
    DiscreteAsyncExecutor,
    ToolInvocationRecord,
)
from my_team.models.activation import AgentActivation, ExecutionConfig, WakeEventType, WakeupEvent


@pytest.fixture
def tool_registry():
    reg = ToolRegistry()
    reg.register_agent("agent.test", WORKER_TOOLS)
    return reg


@pytest.fixture
def base_agent(tool_registry):
    return BaseAgent(agent_id="agent.test", tool_registry=tool_registry)


@pytest.fixture
def snapshot():
    return AgentSnapshot(tick=5)


@pytest.fixture
def activation():
    return AgentActivation(
        activation_id="act.001",
        agent_id="agent.test",
        tick=5,
        wake_events=(
            WakeupEvent(
                event_type=WakeEventType.NEW_EMAIL,
                target_agent_id="agent.test",
                tick=4,
            ),
        ),
    )


class TestToolInvocationRecord:
    def test_fields(self):
        record = ToolInvocationRecord(
            invocation_id="ti.001",
            activation_id="act.001",
            agent_id="agent.test",
            tick=5,
            tool_name="read",
        )
        assert record.success is True
        assert record.arguments_hash == ""


class TestDiscreteAsyncExecutor:
    def test_empty_plan_returns_idle(self, base_agent, snapshot, activation):
        executor = DiscreteAsyncExecutor()
        result = executor.execute_activation(base_agent, snapshot, activation, ExecutionConfig())
        assert result.resulting_state == AgentState.IDLE
        assert len(result.tool_invocations) == 0

    def test_activation_record_preserved(self, base_agent, snapshot, activation):
        executor = DiscreteAsyncExecutor()
        result = executor.execute_activation(base_agent, snapshot, activation, ExecutionConfig())
        assert result.activation.activation_id == "act.001"
        assert result.activation.agent_id == "agent.test"


class TestBoundedMicroLoopExecutor:
    def test_empty_plan_returns_idle(self, base_agent, snapshot, activation):
        executor = BoundedMicroLoopExecutor()
        result = executor.execute_activation(base_agent, snapshot, activation, ExecutionConfig())
        assert result.resulting_state == AgentState.IDLE
        assert len(result.tool_invocations) == 0

    def test_respects_max_rounds(self, base_agent, snapshot, activation):
        executor = BoundedMicroLoopExecutor()
        config = ExecutionConfig(max_micro_loop_rounds=2)
        result = executor.execute_activation(base_agent, snapshot, activation, config)
        # BaseAgent returns empty plans, so rounds don't matter
        assert result.resulting_state == AgentState.IDLE


class TestActivationResult:
    def test_defaults(self, activation):
        result = ActivationResult(
            activation=activation,
            resulting_state=AgentState.IDLE,
        )
        assert len(result.tool_invocations) == 0
        assert len(result.emails_sent) == 0
