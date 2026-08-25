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
from my_team.consolidation import (
    CONSOLIDATION_DIRECTIVE,
    parse_consolidation_output,
    parse_consolidation_request,
)
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import (
    Intent,
    MemoryConsolidateIntent,
    SubmitLLMRequest,
)
from my_team.models.llm import ChatMessage
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

        N4-4（CONSOLIDATING 整理模式）：
        - CONSOLIDATING 相位下 LLM 请求 = 记忆工具集（工具面收窄由
          ToolRegistry 相位门保证）+ 整理指令（CONSOLIDATION_DIRECTIVE）；
        - CONSOLIDATING 输出 = 整理动作（tool_calls）→ SubmitToolRequest
          + 结构化摘要/自决退出（内容 JSON）→ MemoryConsolidateIntent(exit)；
        - 普通模式下内容含主动整理请求标记 → MemoryConsolidateIntent(enter)
          （主动触发，不限于预算满）。
        """
        # N4-4：是否处于 CONSOLIDATING 会话。会话从 enter_consolidating
        # 起至 exit_consolidating 止，跨越 CONSOLIDATING / WAITING_FOR_LLM /
        # PROCESSING_RESULT 等相位——resume_phase 在会话期间持续置位，
        # 是会话的稳定标记（响应处理期 phase 为 PROCESSING_RESULT，
        # 不能以 phase == CONSOLIDATING 判断）。
        consolidating = (
            continuation is not None
            and continuation.resume_phase is not None
        )

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
            intents = action_plan_to_intents(plan)

            if consolidating:
                # CONSOLIDATING 输出契约：整理动作 + 结构化摘要 + exit 标志
                output = parse_consolidation_output(content)
                if output.summary is not None or output.exit_requested:
                    intents.append(MemoryConsolidateIntent(
                        agent_id=self._agent_id,
                        action="exit",
                        reason="consolidation complete",
                        structured_summary=(
                            output.summary.model_dump() if output.summary else None
                        ),
                    ))
            elif parse_consolidation_request(content):
                # 主动触发（不限于预算满）
                intents.append(MemoryConsolidateIntent(
                    agent_id=self._agent_id,
                    action="enter",
                    reason="agent-initiated",
                ))
            return intents

        # Case 2: no pending result → submit async LLM request
        role = "agent"
        if self._agent_config:
            role = getattr(self._agent_config, "role", "agent")

        messages = self._templates.render_system_prompt(
            agent_id=self._agent_id,
            role=role,
            observation=observation,
        )
        if consolidating:
            # N4-4：整理指令（输出契约：动作序列 + 结构化摘要 + 自决退出）
            messages = [
                ChatMessage(role=m.role, content=m.content + "\n\n" + CONSOLIDATION_DIRECTIVE)
                for m in messages
            ]
        # N1b（§5.1）：LLM 只见被两层 Grant 授权的工具（deny-by-default，
        # §3.5）；定义仍从 manifest 自动生成（manifest_to_tool_definition）。
        # N4-4：CONSOLIDATING 下 authorized_tools 已收窄为记忆工具集。
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
