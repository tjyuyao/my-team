"""Agent Continuation — resumable state for continuation-based agents.

Per the architectural redesign: an agent's state is not just "idle" or
"processing" — it includes a resumable continuation that captures where
the agent left off in its ReAct cycle.

When an agent is activated:
  1. System loads its continuation
  2. Continuation tells the agent what happened since last activation
  3. Agent produces the next Intent(s)
  4. System saves the updated continuation

This enables:
  - Agents that span multiple ticks
  - Non-blocking LLM/tool requests
  - Exact restart from any point
  - Audit trail of the ReAct cycle
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ContinuationPhase(str, Enum):
    """Phase of the agent's ReAct continuation."""

    FRESH = "fresh"                    # No prior state; first activation
    WAITING_FOR_LLM = "waiting_for_llm"  # LLM request in flight
    WAITING_FOR_TOOL = "waiting_for_tool"  # Tool request in flight
    WAITING_FOR_CHILD = "waiting_for_child"  # Waiting for child task
    WAITING_FOR_MAIL = "waiting_for_mail"  # Waiting for email
    WAITING_FOR_HUMAN = "waiting_for_human"  # Waiting for human
    WAITING_FOR_EXTERNAL = "waiting_for_external"  # T9: awaiting outbound op
    PROCESSING_RESULT = "processing_result"  # Processing a received result
    READY_TO_DECIDE = "ready_to_decide"  # Ready for next decision
    CONSOLIDATING = "consolidating"  # 记忆整理模式（N4-4，SPEC §4.4）
    COMPLETED = "completed"            # Task completed
    FAILED = "failed"                  # Task failed


class AgentContinuation(BaseModel):
    """Resumable state for an agent's ReAct cycle.

    This is the "memory" of where the agent left off. It is saved
    after each activation and restored at the next activation.
    """

    agent_id: str = Field(description="Agent this continuation belongs to")
    task_id: str = Field(default="", description="Current task being worked on")
    activation_id: str = Field(
        default="",
        description="Last activation ID",
    )
    phase: ContinuationPhase = Field(
        default=ContinuationPhase.FRESH,
        description="Current phase of the ReAct cycle",
    )
    context_version: int = Field(
        default=0,
        ge=0,
        description="Version of the context snapshot used in last activation",
    )
    # N4-4 整理模式（CONSOLIDATING）：进入前记住被打断的相位，退出后恢复
    resume_phase: ContinuationPhase | None = Field(
        default=None,
        description="进入 CONSOLIDATING 前被打断的相位（退出后恢复，续上被打断的工作）",
    )

    # Pending external operation (if any)
    pending_request_id: str = Field(
        default="",
        description="ID of pending LLM/tool request (if waiting)",
    )
    pending_request_type: str = Field(
        default="",
        description="Type of pending request (llm/tool/email/etc)",
    )

    # ReAct cycle tracking
    react_turn: int = Field(
        default=0,
        ge=0,
        description="Current ReAct turn number within this task",
    )
    total_llm_calls: int = Field(
        default=0,
        ge=0,
        description="Total LLM calls in this continuation",
    )
    total_tool_calls: int = Field(
        default=0,
        ge=0,
        description="Total tool calls in this continuation",
    )

    # Last results (for the agent to process)
    last_llm_result: dict[str, Any] = Field(
        default_factory=dict,
        description="Last LLM response (if processing_result phase)",
    )
    last_tool_result: dict[str, Any] = Field(
        default_factory=dict,
        description="Last tool result (if processing_result phase)",
    )
    last_email: dict[str, Any] = Field(
        default_factory=dict,
        description="Last received email (if processing_result phase)",
    )

    # History (for audit and debugging)
    event_log: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Log of events in this continuation",
    )

    def advance_to_waiting_llm(self, request_id: str, tick: int) -> None:
        """Transition to WAITING_FOR_LLM phase."""
        self.phase = ContinuationPhase.WAITING_FOR_LLM
        self.pending_request_id = request_id
        self.pending_request_type = "llm"
        self.total_llm_calls += 1
        self._log("waiting_for_llm", request_id=request_id, tick=tick)

    def advance_to_waiting_tool(self, request_id: str, tick: int) -> None:
        """Transition to WAITING_FOR_TOOL phase."""
        self.phase = ContinuationPhase.WAITING_FOR_TOOL
        self.pending_request_id = request_id
        self.pending_request_type = "tool"
        self.total_tool_calls += 1
        self._log("waiting_for_tool", request_id=request_id, tick=tick)

    def advance_to_waiting_external(self, request_id: str, tick: int) -> None:
        """Transition to WAITING_FOR_EXTERNAL phase (T9 outbound op)."""
        self.phase = ContinuationPhase.WAITING_FOR_EXTERNAL
        self.pending_request_id = request_id
        self.pending_request_type = "external"
        self.total_tool_calls += 1
        self._log("waiting_for_external", request_id=request_id, tick=tick)

    def receive_llm_result(self, result: dict[str, Any], tick: int) -> None:
        """Receive LLM result and transition to PROCESSING_RESULT."""
        self.phase = ContinuationPhase.PROCESSING_RESULT
        self.last_llm_result = result
        self.pending_request_id = ""
        self.pending_request_type = ""
        self.react_turn += 1
        self._log("llm_result_received", tick=tick)

    def finalize_result_processing(self, tick: int) -> None:
        """Reset phase after the agent has processed the received result.

        Called after decide_intents() consumes last_llm_result. Clears
        the result so the next activation starts fresh (either a new
        LLM request or new tool intents).
        """
        if self.phase == ContinuationPhase.PROCESSING_RESULT:
            self.phase = ContinuationPhase.READY_TO_DECIDE
            self.last_llm_result = {}
            self.last_tool_result = {}
            self._log("result_processed", tick=tick)

    def receive_tool_result(self, result: dict[str, Any], tick: int) -> None:
        """Receive tool result and transition to PROCESSING_RESULT."""
        self.phase = ContinuationPhase.PROCESSING_RESULT
        self.last_tool_result = result
        self.pending_request_id = ""
        self.pending_request_type = ""
        self.react_turn += 1
        self._log("tool_result_received", tick=tick)

    def receive_external_result(self, result: dict[str, Any], tick: int) -> None:
        """Receive an outbound op result (T9) → PROCESSING_RESULT."""
        self.phase = ContinuationPhase.PROCESSING_RESULT
        self.last_tool_result = result
        self.pending_request_id = ""
        self.pending_request_type = ""
        self.react_turn += 1
        self._log("external_result_received", tick=tick)

    def mark_completed(self, tick: int) -> None:
        """Mark continuation as completed."""
        self.phase = ContinuationPhase.COMPLETED
        self._log("completed", tick=tick)

    def mark_failed(self, reason: str, tick: int) -> None:
        """Mark continuation as failed."""
        self.phase = ContinuationPhase.FAILED
        self._log("failed", reason=reason, tick=tick)

    # ------------------------------------------------------------------
    # N4-4 整理模式 CONSOLIDATING（SPEC §4.4 / N4_MEMORY_INJECTION_DESIGN §5）
    # ------------------------------------------------------------------

    def enter_consolidating(self, tick: int) -> None:
        """进入 CONSOLIDATING：记住被打断的相位。

        相位迁移在 decide/act（写路径）；Observe 只读消费
        pending_consolidation 标志，不在此迁移。

        会话标记 = ``resume_phase``：会话跨越 CONSOLIDATING /
        WAITING_FOR_LLM / PROCESSING_RESULT / READY_TO_DECIDE 等相位
        （响应处理期 phase 为 PROCESSING_RESULT，finalize 后回落
        READY_TO_DECIDE），resume_phase 在会话期间持续置位。重复进入
        （resume_phase 已置位）为 no-op。
        """
        if self.phase == ContinuationPhase.CONSOLIDATING or self.resume_phase is not None:
            return
        self.resume_phase = self.phase
        self.phase = ContinuationPhase.CONSOLIDATING
        self._log("enter_consolidating", resume_phase=self.resume_phase.value, tick=tick)

    def exit_consolidating(self, tick: int) -> None:
        """退出 CONSOLIDATING：恢复被打断的相位（resume_phase）。

        退出路径：agent 自决（MemoryConsolidateIntent exit）或预算回落
        阈值下（hysteresis）。以会话标记（resume_phase）判定——处理完
        整理响应后 phase 可能已回落 READY_TO_DECIDE，仍须正确恢复
        resume_phase。非会话中调用为 no-op（resume_phase 为 None ⟺
        不在整理会话）。
        """
        if self.resume_phase is None:
            return
        self.phase = self.resume_phase
        self.resume_phase = None
        self._log("exit_consolidating", resumed_phase=self.phase.value, tick=tick)

    def _log(self, event: str, **kwargs: Any) -> None:
        """Append to event log."""
        self.event_log.append({"event": event, **kwargs})

    @property
    def is_waiting(self) -> bool:
        """Check if agent is waiting for an external result."""
        return self.phase in {
            ContinuationPhase.WAITING_FOR_LLM,
            ContinuationPhase.WAITING_FOR_TOOL,
            ContinuationPhase.WAITING_FOR_CHILD,
            ContinuationPhase.WAITING_FOR_MAIL,
            ContinuationPhase.WAITING_FOR_HUMAN,
            ContinuationPhase.WAITING_FOR_EXTERNAL,
        }

    @property
    def is_terminal(self) -> bool:
        """Check if continuation is in a terminal state."""
        return self.phase in {
            ContinuationPhase.COMPLETED,
            ContinuationPhase.FAILED,
        }

    @property
    def has_pending_request(self) -> bool:
        """Check if there is a pending external request."""
        return bool(self.pending_request_id)
