"""Prompt templates for LLM agent interactions.

Renders system prompts, tool definitions, and parses LLM responses
into strict ActionPlan objects.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from my_team.agent_runtime import ActionPlan, AgentAction, AgentObservation
from my_team.models.llm import ChatMessage, ToolDefinition
from my_team.tool_manifest import ToolManifest, manifest_to_tool_definition


class PromptTemplates:
    """Manages prompt templates for different agent roles and phases."""

    def render_system_prompt(
        self,
        agent_id: str,
        role: str,
        observation: AgentObservation,
    ) -> list[ChatMessage]:
        """Render system prompt + context for the LLM.

        Returns a list of ChatMessages ready for the LLM API.

        N1b（§5.1）：``role`` 参数保留仅作兼容（``AgentConfig.role``
        已标注弃用）；提示文案不再注入 role——权限 = 两层 Grant
        （§3.5），role 不是权限依据（§1.8/§4.1）。
        """
        system_content = (
            f"You are agent '{agent_id}'.\n"
            f"You operate in a discrete-time multi-agent simulation.\n"
            f"Current tick: {observation.tick}\n\n"
            f"Rules:\n"
            f"- You can only use tools you are authorized for.\n"
            f"- Produce exactly one ActionPlan with 0 or more actions.\n"
            f"- Do not attempt to access other agents' private spaces.\n"
            f"- All actions go through the system for validation.\n"
        )

        # Add task context
        if observation.task_states:
            task_lines = []
            for task_id, state in observation.task_states.items():
                task_lines.append(
                    f"  - {task_id}: status={state.get('status', 'unknown')}, "
                    f"title={state.get('title', '')}"
                )
            system_content += "\nYour tasks:\n" + "\n".join(task_lines) + "\n"

        # Add email context
        if observation.emails:
            email_lines = []
            for email in observation.emails:
                email_lines.append(
                    f"  - From: {email.get('from', '?')}, "
                    f"Type: {email.get('email_type', '?')}, "
                    f"Subject: {email.get('subject', '')}"
                )
            system_content += "\nNew emails:\n" + "\n".join(email_lines) + "\n"

        return [ChatMessage(role="system", content=system_content)]

    def render_tool_definitions(
        self,
        authorized_tools: frozenset[str],
        manifests: Mapping[str, ToolManifest] | None = None,
    ) -> list[ToolDefinition]:
        """Render tool definitions for LLM function calling.

        Definitions are GENERATED from ToolManifests via
        manifest_to_tool_definition (v0.10 T7) — no hand-written tool
        table. Only tools the agent is AUTHORIZED to use (two-layer
        Grant, §3.5/§5.1) AND that have a registered manifest are
        included. Unknown / manifest-less tools yield no definition
        (they cannot be invoked safely).

        N1b：参数 ``authorized_tools``（原 ``allowed_tools``）——白名单
        语义废除，调用方传两层 Grant 求值后的授权工具集。
        """
        if manifests is None:
            manifests = {}
        return [
            manifest_to_tool_definition(manifests[name])
            for name in sorted(authorized_tools)
            if name in manifests
        ]

    def parse_llm_response(
        self,
        content: str,
        tool_calls: list[dict[str, Any]],
        agent_id: str,
        tick: int,
    ) -> ActionPlan:
        """Parse LLM response into a strict ActionPlan.

        Unknown actions are rejected with an error action.
        """
        actions: list[AgentAction] = []

        # Parse tool calls from LLM
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            actions.append(AgentAction(
                action_type=tool_name,
                tool_name=tool_name,
                payload=args,
            ))

        # If LLM returned text content, treat as a potential email/message
        if content and not actions:
            # LLM decided to respond with text — no tool calls
            pass

        return ActionPlan(
            agent_id=agent_id,
            tick=tick,
            actions=actions,
        )
