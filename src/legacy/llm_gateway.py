"""LLM Gateway: routes LLM calls to per-agent providers via litellm.

Per SPEC §8.4: all LLM calls go through this gateway. Tracks invocations
for audit. Provider configs are frozen at init time — agents cannot modify.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from my_team.budget import estimate_cost
from my_team.models.llm import (
    LLMInvocation,
    LLMProviderConfig,
    LLMRequest,
    LLMResult,
)
from pydantic import BaseModel, Field


class LLMGatewayConfig(BaseModel):
    """Global gateway configuration."""

    allowed_api_bases: list[str] = Field(
        default_factory=list,
        description="Allowlist of allowed api_base URLs. Empty = allow all.",
    )
    log_level: str = Field(default="info", description="Logging level")


class LLMGateway:
    """Centralized LLM call gateway with per-agent provider routing.

    Uses litellm underneath for multi-provider support.
    Provider configs are registered at init time and cannot be modified by agents.

    Security:
    - API keys read from env at init, never stored in model or log
    - api_base validated against allowlist
    - complete() uses activation context, not caller-provided agent_id
    """

    def __init__(self, config: LLMGatewayConfig | None = None) -> None:
        self._config = config or LLMGatewayConfig()
        self._profiles: dict[str, LLMProviderConfig] = {}
        self._agent_profiles: dict[str, str] = {}  # agent_id → profile_name
        self._invocation_log: list[LLMInvocation] = []
        self._api_keys: dict[str, str] = {}  # profile_name → resolved key

    @property
    def config(self) -> LLMGatewayConfig:
        return self._config

    def register_profile(
        self,
        profile_name: str,
        provider_config: LLMProviderConfig,
    ) -> None:
        """Register an LLM provider profile. Frozen after registration."""
        self._profiles[profile_name] = provider_config

        # Resolve API key from environment
        if provider_config.api_key_env:
            key = os.environ.get(provider_config.api_key_env, "")
            if not key:
                raise ValueError(
                    f"Environment variable '{provider_config.api_key_env}' "
                    f"not set for profile '{profile_name}'"
                )
            self._api_keys[profile_name] = key

        # Validate api_base against allowlist
        if provider_config.api_base and self._config.allowed_api_bases:
            if provider_config.api_base not in self._config.allowed_api_bases:
                raise ValueError(
                    f"api_base '{provider_config.api_base}' not in allowlist "
                    f"for profile '{profile_name}'"
                )

    def bind_agent(self, agent_id: str, profile_name: str) -> None:
        """Bind an agent to a provider profile."""
        if profile_name not in self._profiles:
            raise ValueError(f"Profile '{profile_name}' not registered")
        self._agent_profiles[agent_id] = profile_name

    def _get_profile(self, agent_id: str) -> LLMProviderConfig:
        """Get the provider config for an agent."""
        profile_name = self._agent_profiles.get(agent_id)
        if profile_name is None:
            raise ValueError(f"No LLM profile bound to agent '{agent_id}'")
        profile = self._profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"Profile '{profile_name}' not found")
        return profile

    def complete(self, request: LLMRequest) -> LLMResult:
        """Send a completion request through the gateway.

        Routes to the correct provider based on agent's profile.
        Tracks the invocation for audit.
        """
        profile = self._get_profile(request.agent_id)
        invocation_id = f"llm.{uuid.uuid4().hex[:12]}"

        try:
            result = self._call_provider(profile, request)

            invocation = LLMInvocation(
                invocation_id=invocation_id,
                activation_id=request.activation_id,
                agent_id=request.agent_id,
                tick=0,  # set by caller
                model=profile.model,
                provider=profile.provider,
                input_tokens=result.usage.get("prompt_tokens", 0),
                output_tokens=result.usage.get("completion_tokens", 0),
                # T16c: cost derived from the pricing table (models/llm.py
                # declares the field; the budget judgment is cost-first).
                cost=estimate_cost(
                    profile.model,
                    result.usage.get("prompt_tokens", 0),
                    result.usage.get("completion_tokens", 0),
                ),
                success=True,
                finish_reason=result.finish_reason,
            )
            self._invocation_log.append(invocation)
            return result

        except Exception as e:
            invocation = LLMInvocation(
                invocation_id=invocation_id,
                activation_id=request.activation_id,
                agent_id=request.agent_id,
                tick=0,
                model=profile.model,
                provider=profile.provider,
                success=False,
                error=str(e),
            )
            self._invocation_log.append(invocation)
            raise

    def _call_provider(
        self,
        profile: LLMProviderConfig,
        request: LLMRequest,
    ) -> LLMResult:
        """Call the LLM provider. Routes by provider name."""
        # Try litellm first
        try:
            import litellm

            kwargs: dict[str, Any] = {
                "model": f"{profile.provider}/{profile.model}"
                if profile.provider != "ollama"
                else profile.model,
                "messages": [m.model_dump() for m in request.messages],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            }

            if request.tools:
                kwargs["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        },
                    }
                    for t in request.tools
                ]

            if profile.api_base:
                kwargs["api_base"] = profile.api_base

            response = litellm.completion(**kwargs)

            content = ""
            tool_calls: list[dict[str, Any]] = []
            if response.choices:
                msg = response.choices[0].message
                content = msg.content or ""
                if msg.tool_calls:
                    tool_calls = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ]

            usage = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            return LLMResult(
                content=content,
                tool_calls=tool_calls,
                usage=usage,
                model=response.model or profile.model,
                finish_reason=response.choices[0].finish_reason if response.choices else None,
            )

        except ImportError:
            raise RuntimeError(
                "litellm is required for LLM gateway. "
                "Install with: pip install litellm"
            )

    def get_invocation_log(self) -> list[LLMInvocation]:
        """Return all LLM invocations for audit."""
        return list(self._invocation_log)

    def get_profile(self, profile_name: str) -> LLMProviderConfig | None:
        """Get a provider profile by name."""
        return self._profiles.get(profile_name)

    def get_agent_profile(self, agent_id: str) -> str | None:
        """Get the profile name bound to an agent."""
        return self._agent_profiles.get(agent_id)
