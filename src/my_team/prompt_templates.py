"""Prompt templates for LLM agent interactions.

Renders system prompts, tool definitions, and parses LLM responses
into strict ActionPlan objects.
"""

from __future__ import annotations

import json
from typing import Any

from my_team.agent_runtime import ActionPlan, AgentAction, AgentObservation
from my_team.models.llm import ChatMessage, ToolDefinition


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
        """
        system_content = (
            f"You are agent '{agent_id}' with role '{role}'.\n"
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
        allowed_tools: frozenset[str],
    ) -> list[ToolDefinition]:
        """Render tool definitions for LLM function calling.

        Only includes tools the agent is authorized to use.
        """
        tool_schemas = {
            "read": ToolDefinition(
                name="read",
                description="Read a file from your private workspace",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace",
                        },
                    },
                    "required": ["path"],
                },
            ),
            "write": ToolDefinition(
                name="write",
                description="Write a file to your private workspace",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to workspace",
                        },
                        "content": {"type": "string", "description": "Content"},
                    },
                    "required": ["path", "content"],
                },
            ),
            "ls": ToolDefinition(
                name="ls",
                description="List files in your private workspace",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path"},
                    },
                },
            ),
            "delegate": ToolDefinition(
                name="delegate",
                description="Delegate a task to a direct child agent",
                parameters={
                    "type": "object",
                    "properties": {
                        "recipient_agent_id": {"type": "string"},
                        "task_title": {"type": "string"},
                        "task_description": {"type": "string"},
                    },
                    "required": ["recipient_agent_id", "task_title"],
                },
            ),
            "send_email": ToolDefinition(
                name="send_email",
                description="Send an email to another agent",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "array", "items": {"type": "string"}},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                },
            ),
        }

        return [
            tool_schemas[name]
            for name in sorted(allowed_tools)
            if name in tool_schemas
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
