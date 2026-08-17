"""Deterministic fake LLM provider for scripted simulations and tests.

Simulates an external LLM provider that:
- Accepts SubmitLLMRequest pending operations
- Completes them after a configurable latency (in ticks)
- Returns scripted responses per (agent_id, call_index)

This lets E2E tests drive the full async LLM loop without a real
provider, while preserving deterministic replay semantics.

Integration: call `provider.advance(tick)` before each `sim.run_tick()`
— it completes ops whose simulated response has "arrived". The
simulation's Phase 1 (Ingest) then collects them as LLM_RESULT events.
"""

from __future__ import annotations

from typing import Any

from my_team.pending_ops import OpStatus, OpType


class FakeLLMProvider:
    """Scripted LLM provider with per-agent response sequences.

    Responses are keyed by agent_id; each entry is a list of
    {content, tool_calls} dicts returned in order of calls.
    If an agent exhausts its script, subsequent calls return empty.
    """

    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]] | None = None,
        latency_ticks: int = 1,
    ) -> None:
        self._responses: dict[str, list[dict[str, Any]]] = responses or {}
        self._latency_ticks = latency_ticks
        self._call_counters: dict[str, int] = {}

    @property
    def latency_ticks(self) -> int:
        return self._latency_ticks

    def register_script(
        self,
        agent_id: str,
        responses: list[dict[str, Any]],
    ) -> None:
        """Register a deterministic response script for an agent."""
        self._responses[agent_id] = responses

    def _next_response(self, agent_id: str) -> dict[str, Any]:
        """Get the next scripted response for an agent (deterministic)."""
        idx = self._call_counters.get(agent_id, 0)
        self._call_counters[agent_id] = idx + 1
        script = self._responses.get(agent_id, [])
        if idx < len(script):
            return script[idx]
        return {"content": "", "tool_calls": []}

    def advance(self, simulation: Any, current_tick: int) -> int:
        """Complete LLM operations whose simulated response has arrived.

        Called by the test harness before each sim.run_tick().

        Returns the number of ops completed.
        """
        completed = 0
        registry = simulation._pending_ops
        for op in registry._operations.values():
            if op.op_type != OpType.LLM_REQUEST:
                continue
            if op.status not in {OpStatus.SUBMITTED, OpStatus.PENDING}:
                continue
            # Simulated response arrival: created_tick + latency
            if current_tick >= op.created_tick + self._latency_ticks:
                response = self._next_response(op.agent_id)
                registry.complete(op.request_id, result=response)
                completed += 1
        return completed
