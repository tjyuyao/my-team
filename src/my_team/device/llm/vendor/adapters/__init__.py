"""Vendored provider 适配层（来自上游 huggingface/tau，裁剪至两个 provider）。

对外暴露：协议适配器与配置。
"""

from my_team.device.llm.vendor.adapters.anthropic import AnthropicProvider
from my_team.device.llm.vendor.adapters.env import (
    DEFAULT_ANTHROPIC_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    AnthropicConfig,
    OpenAICompatibleConfig,
    RuntimeProviderAuth,
    openai_compatible_config_from_env,
)
from my_team.device.llm.vendor.adapters.model_limits import (
    ModelLimitsProvider,
    RuntimeModelLimits,
)
from my_team.device.llm.vendor.adapters.openai_compatible import OpenAICompatibleProvider
from my_team.device.llm.vendor.adapters.provider import CancellationToken, ModelProvider

__all__ = [
    "AnthropicProvider",
    "OpenAICompatibleProvider",
    "AnthropicConfig",
    "OpenAICompatibleConfig",
    "RuntimeProviderAuth",
    "openai_compatible_config_from_env",
    "DEFAULT_ANTHROPIC_BASE_URL",
    "DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES",
    "DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS",
    "DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS",
    "CancellationToken",
    "ModelProvider",
    "ModelLimitsProvider",
    "RuntimeModelLimits",
]
