"""LLM-backed agent implementation using the AgentRuntime protocol.

Per SPEC §8.6 (v0.6.0): LLM calls are NON-BLOCKING. The agent never
waits for a model response inside decide(). Instead:

1. If continuation has a pending LLM result → parse it into Intents
2. Otherwise → produce SubmitLLMRequest intent (async)

The system registers the request, the agent transitions to
WAITING_FOR_LLM, and the response arrives as an LLM_RESULT WakeEvent
in a future tick.
"""

from __future__ import annotations

from typing import Any

from my_team.agent_runtime import (
    AgentObservation,
    BaseAgent,
    ToolRegistry,
    action_plan_to_intents,
)
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SubmitLLMRequest
from my_team.prompt_templates import PromptTemplates


class LLMAgent(BaseAgent):
    """Agent that uses LLM for the Decide phase.

    v0.6.0 behavior:
    - decide_intents() NEVER blocks on a model call
    - If a pending LLM result exists in the continuation, parse it
    - Otherwise produce SubmitLLMRequest (async)
    """

    def __init__(
        self,
        agent_id: str,
        llm_gateway: Any,
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

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        """Produce non-blocking Intents.

        Flow:
        1. If continuation has a pending LLM result → parse it into
           ActionPlan → convert to Intents
        2. Otherwise → SubmitLLMRequest (async, does NOT block)
        """
        # Case 1: processing a received LLM result
        if (
            continuation is not None
            and continuation.phase == ContinuationPhase.PROCESSING_RESULT
            and continuation.last_llm_result
        ):
            result = continuation.last_llm_result
            content = result.get("content", "")
            tool_calls = result.get("tool_calls", [])

            plan = self._templates.parse_llm_response(
                content=content,
                tool_calls=list(tool_calls),
                agent_id=self._agent_id,
                tick=observation.tick,
            )
            return action_plan_to_intents(plan)

        # Case 2: no pending result → submit async LLM request
        role = "agent"
        if self._agent_config:
            role = getattr(self._agent_config, "role", "agent")

        messages = self._templates.render_system_prompt(
            agent_id=self._agent_id,
            role=role,
            observation=observation,
        )
        # N1b（§5.1）：LLM 只见被两层 Grant 授权的工具（deny-by-default，
        # §3.5）；定义仍从 manifest 自动生成（manifest_to_tool_definition）。
        tools = self._templates.render_tool_definitions(
            self._tool_registry.authorized_tools(self._agent_id),
            manifests={m.name: m for m in self._tool_registry.manifests()},
        )

        request = SubmitLLMRequest(
            agent_id=self._agent_id,
            task_id=continuation.task_id if continuation else "",
            model=self._llm_profile,
            messages=tuple(
                {k: v for k, v in m.model_dump().items() if v is not None}
                for m in messages
            ),
            tools=tuple(t.model_dump() for t in tools),
            temperature=0.7,
            max_tokens=4096,
            timeout_ticks=10,
        )
        return [request]
