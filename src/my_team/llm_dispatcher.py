"""LLM Dispatcher — automatic LLM op completion via LLMGateway.

Worker thread that polls SUBMITTED LLM operations, calls the LLMGateway,
and writes results back to the PendingOperationRegistry. Replaces the
manual FakeLLMProvider.advance() pattern for production use.

FakeLLMProvider.advance() still works for deterministic testing — the
dispatcher only processes ops that haven't been completed yet.

Usage:
    sim = Simulation(agent_tree=tree)
    gateway = LLMGateway()
    dispatcher = LLMDispatcher(sim, gateway)
    dispatcher.start()
    # ... LLM ops are automatically completed ...
    dispatcher.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from my_team.llm_gateway import LLMGateway
from my_team.models.llm import LLMRequest
from my_team.pending_ops import OpStatus, OpType

logger = logging.getLogger(__name__)


class LLMDispatcher:
    """Polls SUBMITTED LLM ops and completes them via LLMGateway.

    Runs in a background thread. Each poll cycle:
    1. Scan PendingOperationRegistry for SUBMITTED LLM_REQUEST ops
    2. For each: build LLMRequest, call gateway.complete(), write result
    3. On failure: mark op as failed with error details
    """

    def __init__(
        self,
        simulation: Any,
        gateway: LLMGateway,
        poll_interval: float = 0.5,
    ) -> None:
        self._sim = simulation
        self._gateway = gateway
        self._poll_interval = max(0.05, poll_interval)
        self._running = False
        self._thread: threading.Thread | None = None
        self._processed_count = 0
        self._error_count = 0

    @property
    def processed_count(self) -> int:
        return self._processed_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def start(self) -> None:
        """Start the dispatcher worker thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="llm-dispatcher",
                daemon=True,
            )
            self._thread.start()
        logger.info("LLM Dispatcher started (poll_interval=%.2fs)", self._poll_interval)

    def stop(self) -> None:
        """Stop the dispatcher and wait for the current poll to finish."""
        with self._lock:
            if not self._running:
                return
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info(
            "LLM Dispatcher stopped (processed=%d, errors=%d)",
            self._processed_count,
            self._error_count,
        )

    @property
    def _lock(self) -> threading.Lock:
        # Re-entrant lock for start/stop safety
        if not hasattr(self, "_lock_impl"):
            self._lock_impl = threading.Lock()
        return self._lock_impl

    def _poll_loop(self) -> None:
        """Background poll loop."""
        while self._running:
            try:
                self._poll_once()
            except Exception:
                logger.exception("LLM Dispatcher poll error")
            time.sleep(self._poll_interval)

    def _poll_once(self) -> None:
        """Single poll cycle: find and complete SUBMITTED LLM ops."""
        registry = self._sim._pending_ops
        for op in list(registry._operations.values()):
            if not self._running:
                break
            if op.op_type != OpType.LLM_REQUEST:
                continue
            if op.status != OpStatus.SUBMITTED:
                continue
            self._process_op(op)

    def _process_op(self, op: Any) -> None:
        """Process a single SUBMITTED LLM op."""
        registry = self._sim._pending_ops

        # Build LLMRequest from op metadata
        request = self._build_request(op)
        if request is None:
            logger.warning("Cannot build LLMRequest for op %s", op.request_id)
            registry.fail(op.request_id, error="Cannot build LLM request from op metadata")
            self._error_count += 1
            return

        try:
            result = self._gateway.complete(request)
            # Convert LLMResult to dict for registry.complete()
            result_dict = {
                "content": result.content,
                "tool_calls": result.tool_calls,
                "usage": result.usage,
                "model": result.model,
                "finish_reason": result.finish_reason,
            }
            registry.complete(op.request_id, result=result_dict)
            self._processed_count += 1
            logger.debug("Dispatched LLM op %s for agent %s", op.request_id, op.agent_id)

        except Exception as e:
            logger.error(
                "LLM dispatch failed for op %s: %s", op.request_id, e,
            )
            registry.fail(op.request_id, error=f"LLM dispatch error: {e}")
            self._error_count += 1

    def _build_request(self, op: Any) -> LLMRequest | None:
        """Build an LLMRequest from a PendingOperation."""
        metadata = op.metadata or {}

        # Extract messages from metadata (stored by _phase_act)
        messages_raw = metadata.get("messages", [])
        if not messages_raw:
            # Fallback: try to reconstruct from tool_request
            return None

        from my_team.models.llm import ChatMessage
        messages = tuple(
            ChatMessage(**m) if isinstance(m, dict) else m
            for m in messages_raw
        )

        return LLMRequest(
            request_id=op.request_id,
            agent_id=op.agent_id,
            activation_id=op.activation_id or "",
            messages=messages,
            tools=tuple(metadata.get("tools", [])),
            temperature=metadata.get("temperature", 0.7),
            max_tokens=metadata.get("max_tokens", 4096),
        )

    def __repr__(self) -> str:
        return (
            f"LLMDispatcher(processed={self._processed_count}, "
            f"errors={self._error_count}, running={self._running})"
        )
