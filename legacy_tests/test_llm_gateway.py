"""Tests for LLM Gateway: provider routing, config, security.

Uses mock LLM responses — no real API calls.
"""

import os

import pytest

from my_team.llm_gateway import LLMGateway, LLMGatewayConfig
from my_team.models.llm import (
    ChatMessage,
    LLMProviderConfig,
    LLMRequest,
)


class TestLLMGatewayConfig:
    def test_defaults(self):
        cfg = LLMGatewayConfig()
        assert cfg.allowed_api_bases == []

    def test_custom_config(self):
        cfg = LLMGatewayConfig(allowed_api_bases=["https://custom.api.com"])
        assert "https://custom.api.com" in cfg.allowed_api_bases


class TestLLMProviderConfig:
    def test_defaults(self):
        cfg = LLMProviderConfig(provider="openai", model="gpt-4o")
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 4096
        assert cfg.api_key_env == ""

    def test_bounds_enforced(self):
        with pytest.raises(Exception):
            LLMProviderConfig(provider="openai", model="gpt-4o", temperature=3.0)
        with pytest.raises(Exception):
            LLMProviderConfig(provider="openai", model="gpt-4o", max_tokens=0)


class TestLLMGateway:
    def test_register_profile(self):
        gw = LLMGateway()
        gw.register_profile("default", LLMProviderConfig(
            provider="openai",
            model="gpt-4o",
        ))
        assert gw.get_profile("default") is not None

    def test_bind_agent(self):
        gw = LLMGateway()
        gw.register_profile("default", LLMProviderConfig(
            provider="openai",
            model="gpt-4o",
        ))
        gw.bind_agent("agent.root", "default")
        assert gw.get_agent_profile("agent.root") == "default"

    def test_bind_unknown_profile_raises(self):
        gw = LLMGateway()
        with pytest.raises(ValueError, match="not registered"):
            gw.bind_agent("agent.root", "nonexistent")

    def test_complete_without_binding_raises(self):
        gw = LLMGateway()
        gw.register_profile("default", LLMProviderConfig(
            provider="openai",
            model="gpt-4o",
        ))
        request = LLMRequest(
            request_id="req.001",
            agent_id="agent.root",
            activation_id="act.001",
            messages=(ChatMessage(role="user", content="hello"),),
        )
        with pytest.raises(ValueError, match="No LLM profile bound"):
            gw.complete(request)

    def test_api_base_allowlist(self):
        gw = LLMGateway(LLMGatewayConfig(
            allowed_api_bases=["https://allowed.com"],
        ))
        # Allowed
        gw.register_profile("ok", LLMProviderConfig(
            provider="openai",
            model="gpt-4o",
            api_base="https://allowed.com",
        ))
        # Not allowed
        with pytest.raises(ValueError, match="not in allowlist"):
            gw.register_profile("bad", LLMProviderConfig(
                provider="openai",
                model="gpt-4o",
                api_base="https://evil.com",
            ))

    def test_invocation_log_empty(self):
        gw = LLMGateway()
        assert len(gw.get_invocation_log()) == 0

    def test_missing_api_key_env_at_init(self):
        """Missing env var should fail at register_profile time."""
        gw = LLMGateway()
        # Use a definitely-nonexistent env var
        os.environ.pop("NONEXISTENT_KEY_12345", None)
        with pytest.raises(ValueError, match="not set"):
            gw.register_profile("test", LLMProviderConfig(
                provider="openai",
                model="gpt-4o",
                api_key_env="NONEXISTENT_KEY_12345",
            ))

    def test_multiple_profiles(self):
        gw = LLMGateway()
        gw.register_profile("fast", LLMProviderConfig(
            provider="openai", model="gpt-4o-mini",
        ))
        gw.register_profile("smart", LLMProviderConfig(
            provider="anthropic", model="claude-sonnet-4-20250514",
        ))
        gw.bind_agent("agent.root", "smart")
        gw.bind_agent("agent.worker", "fast")
        assert gw.get_agent_profile("agent.root") == "smart"
        assert gw.get_agent_profile("agent.worker") == "fast"
