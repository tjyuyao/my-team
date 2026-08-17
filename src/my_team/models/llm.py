"""LLM invocation tracking and provider configuration models.

Per SPEC §8.4, §10: LLM calls go through the gateway, are tracked
for audit, and providers are configured per-agent via config files.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LLMProviderConfig(BaseModel):
    """Configuration for an LLM provider.

    Loaded at simulation init time and frozen — agents cannot modify.
    API keys are read from environment variables, never stored in config.
    """

    provider: str = Field(
        description="Provider name: 'anthropic', 'openai', 'gemini', 'ollama', etc.",
    )
    model: str = Field(description="Model name, e.g. 'gpt-4o', 'claude-sonnet-4-20250514'")
    api_key_env: str = Field(
        default="",
        description="Environment variable name for API key (never log actual key)",
    )
    api_base: str = Field(
        default="",
        description="Custom endpoint URL (validated against allowlist)",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=128000)


class ChatMessage(BaseModel):
    """A single chat message for LLM input."""

    role: str = Field(description="Message role: 'system', 'user', 'assistant', 'tool'")
    content: str = Field(default="", description="Message content")
    tool_call_id: str | None = Field(
        default=None,
        description="Tool call ID for tool result messages",
    )
    name: str | None = Field(
        default=None,
        description="Sender name for tool messages",
    )


class ToolDefinition(BaseModel):
    """Tool definition for LLM function calling."""

    name: str = Field(description="Tool name")
    description: str = Field(description="Tool description")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for tool parameters",
    )


class LLMRequest(BaseModel):
    """Explicit request model for LLM completion.

    No **kwargs — all parameters are declared and validated.
    """

    request_id: str = Field(description="Unique request identifier")
    agent_id: str = Field(description="Agent making the request")
    activation_id: str = Field(description="Activation this request belongs to")
    messages: tuple[ChatMessage, ...] = Field(description="Chat messages")
    tools: tuple[ToolDefinition, ...] = Field(
        default_factory=tuple,
        description="Available tool definitions",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=128000)


class LLMInvocation(BaseModel):
    """Record of a single LLM API call for audit and replay."""

    invocation_id: str = Field(description="Unique invocation identifier")
    activation_id: str = Field(description="Activation this invocation belongs to")
    agent_id: str = Field(description="Agent that made the call")
    tick: int = Field(description="Tick when invocation was made")
    model: str = Field(description="Model used")
    provider: str = Field(description="Provider used")
    input_tokens: int = Field(default=0, description="Input token count")
    output_tokens: int = Field(default=0, description="Output token count")
    latency_ms: float = Field(default=0, description="Latency in milliseconds")
    success: bool = Field(default=True, description="Whether the call succeeded")
    error: str | None = Field(default=None, description="Error message if failed")
    finish_reason: str | None = Field(
        default=None,
        description="Model's finish reason: 'stop', 'length', 'tool_calls', etc.",
    )
    cost: float = Field(default=0.0, description="Estimated cost in USD")


class LLMResult(BaseModel):
    """Result of an LLM completion."""

    content: str = Field(default="", description="Model's text response")
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Tool calls requested by the model",
    )
    usage: dict[str, int] = Field(
        default_factory=dict,
        description="Token usage: {prompt_tokens, completion_tokens, total_tokens}",
    )
    model: str = Field(default="", description="Model that generated the response")
    finish_reason: str | None = Field(
        default=None,
        description="Finish reason: 'stop', 'length', 'tool_calls', etc.",
    )
