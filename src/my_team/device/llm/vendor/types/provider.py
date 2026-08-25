"""Provider contract owned by Tau's portable agent layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from my_team.device.llm.vendor.types.messages import AgentMessage
from my_team.device.llm.vendor.types.provider_events import AssistantMessageEvent
from my_team.device.llm.vendor.types.tools import AgentTool


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the current stream should stop."""
        ...


class ModelProvider(Protocol):
    """Provider-neutral Pi-compatible model stream interface."""

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Stream one model response as assistant message events.

        Providers may use ``session_id`` for request routing or prompt-cache
        affinity. Unsupported providers ignore it.
        """
        ...
