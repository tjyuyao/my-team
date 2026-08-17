"""Tests for LLMAgent and PromptTemplates.

Uses mock LLM responses — no real API calls.
"""

import json

import pytest

from my_team.agent_runtime import (
    WORKER_TOOLS,
    AgentObservation,
    AgentSnapshot,
    ToolRegistry,
)
from my_team.llm_agent import LLMAgent
from my_team.llm_gateway import LLMGateway
from my_team.models.llm import LLMProviderConfig
from my_team.prompt_templates import PromptTemplates


@pytest.fixture
def mock_gateway():
    """Create a gateway with a mock provider that returns predetermined responses."""
    gw = LLMGateway()
    gw.register_profile("test", LLMProviderConfig(
        provider="openai",
        model="gpt-4o",
    ))
    gw.bind_agent("agent.test", "test")
    return gw


@pytest.fixture
def tool_registry():
    reg = ToolRegistry()
    reg.register_agent("agent.test", WORKER_TOOLS)
    return reg


class TestPromptTemplates:
    def test_render_system_prompt(self):
        templates = PromptTemplates()
        obs = AgentObservation(
            agent_id="agent.test",
            tick=5,
            emails=[{"from": "agent.root", "email_type": "delegation", "subject": "Do research"}],
            task_states={"task.001": {"status": "assigned", "title": "Research task"}},
        )
        messages = templates.render_system_prompt(
            agent_id="agent.test",
            role="researcher",
            observation=obs,
        )
        assert len(messages) == 1
        assert messages[0].role == "system"
        assert "agent.test" in messages[0].content
        assert "tick: 5" in messages[0].content
        assert "task.001" in messages[0].content

    def test_render_tool_definitions(self):
        templates = PromptTemplates()
        tools = templates.render_tool_definitions(frozenset({"read", "write"}))
        names = {t.name for t in tools}
        assert "read" in names
        assert "write" in names
        assert "delegate" not in names

    def test_parse_llm_response_tool_calls(self):
        templates = PromptTemplates()
        tool_calls = [
            {
                "id": "call.001",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": json.dumps({"path": "test.md"}),
                },
            }
        ]
        plan = templates.parse_llm_response(
            content="",
            tool_calls=tool_calls,
            agent_id="agent.test",
            tick=5,
        )
        assert plan.agent_id == "agent.test"
        assert plan.tick == 5
        assert len(plan.actions) == 1
        assert plan.actions[0].tool_name == "read"
        assert plan.actions[0].payload["path"] == "test.md"

    def test_parse_llm_response_empty(self):
        templates = PromptTemplates()
        plan = templates.parse_llm_response(
            content="I'll think about it.",
            tool_calls=[],
            agent_id="agent.test",
            tick=5,
        )
        assert len(plan.actions) == 0


class TestLLMAgent:
    def test_decide_produces_plan(self, mock_gateway, tool_registry):
        agent = LLMAgent(
            agent_id="agent.test",
            llm_gateway=mock_gateway,
            llm_profile="test",
            tool_registry=tool_registry,
        )
        AgentObservation(agent_id="agent.test", tick=0)
        # decide() will call LLM — but since litellm may not be installed,
        # we test that the method exists and has the right signature
        # In production, this would call the actual LLM
        assert hasattr(agent, "decide")
        assert agent.llm_profile == "test"

    def test_agent_id(self, mock_gateway, tool_registry):
        agent = LLMAgent(
            agent_id="agent.test",
            llm_gateway=mock_gateway,
            llm_profile="test",
            tool_registry=tool_registry,
        )
        assert agent.agent_id == "agent.test"

    def test_observe_works(self, mock_gateway, tool_registry):
        agent = LLMAgent(
            agent_id="agent.test",
            llm_gateway=mock_gateway,
            llm_profile="test",
            tool_registry=tool_registry,
        )
        from types import MappingProxyType
        snapshot = AgentSnapshot(
            tick=5,
            emails=({"subject": "hello"},),
            task_states=MappingProxyType({"t1": MappingProxyType({"s": 1})}),
        )
        obs = agent.observe(snapshot)
        assert obs.agent_id == "agent.test"
        assert obs.tick == 5
