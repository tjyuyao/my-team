"""LLM-backed agent implementation using the AgentRuntime protocol.

Per SPEC §10: agent uses LLM for the Decide phase. LLM output is
parsed into a strict ActionPlan. No side effects during parsing.
"""

from __future__ import annotations

import uuid
from typing import Any

from my_team.agent_runtime import (
    ActionPlan,
    AgentObservation,
    BaseAgent,
    ToolRegistry,
)
from my_team.llm_gateway import LLMGateway
from my_team.models.llm import LLMRequest
from my_team.prompt_templates import PromptTemplates


class LLMAgent(BaseAgent):
    """Agent that uses LLM for the Decide phase.

    Integrates with LLMGateway for inference and enforces
    activation-level constraints (max LLM calls, max tool calls).

    The LLM output is parsed into an ActionPlan. Unknown actions
    are rejected — no side effects occur during parsing.
    """

    def __init__(
        self,
        agent_id: str,
        llm_gateway: LLMGateway,
        llm_profile: str,
        tool_registry: ToolRegistry | None = None,
        prompt_templates: PromptTemplates | None = None,
        agent_config: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(agent_id=agent_id, tool_registry=tool_registry, **kwargs)
        self._llm = llm_gateway
        self._llm_profile = llm_profile
        self._templates = prompt_templates or PromptTemplates()
        self._agent_config = agent_config

        # Bind this agent to its LLM profile
        self._llm.bind_agent(agent_id, llm_profile)

    @property
    def llm_profile(self) -> str:
        return self._llm_profile

    def decide(self, observation: AgentObservation) -> ActionPlan:
        """Override decide to use LLM for action planning.

        Flow:
        1. Render prompt via PromptTemplates
        2. Call llm_gateway.complete(request)
        3. Parse response → strict ActionPlan
        4. Unknown actions → reject with error
        """
        # Render messages
        role = "agent"
        if self._agent_config:
            role = getattr(self._agent_config, "role", "agent")

        messages = self._templates.render_system_prompt(
            agent_id=self._agent_id,
            role=role,
            observation=observation,
        )

        # Render tool definitions
        tools = self._templates.render_tool_definitions(
            allowed_tools=self._tool_context.allowed_tools,
        )

        # Build request
        activation_id = f"act.{uuid.uuid4().hex[:12]}"
        request = LLMRequest(
            request_id=f"req.{uuid.uuid4().hex[:12]}",
            agent_id=self._agent_id,
            activation_id=activation_id,
            messages=tuple(messages),
            tools=tuple(tools),
            temperature=0.7,
            max_tokens=4096,
        )

        # Call LLM
        result = self._llm.complete(request)

        # Parse into ActionPlan
        plan = self._templates.parse_llm_response(
            content=result.content,
            tool_calls=result.tool_calls,
            agent_id=self._agent_id,
            tick=observation.tick,
        )

        return plan
