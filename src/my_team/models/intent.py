"""Intent types for the continuation-based agent model.

Per the architectural redesign: ReAct is the agent's behavioral protocol;
Tick is the kernel's advancement protocol. They are not 1:1.

An Intent is what an agent produces during a single activation — a finite,
non-blocking step forward in its ReAct continuation. It is NOT a complete
ReAct trace; it is the next step in the continuation.

Examples:
  - Agent has no pending results → Intent: SubmitLLMRequest
  - Agent has LLM result → Intent: SubmitToolRequest
  - Agent has tool result → Intent: SubmitToolRequest (next step)
  - Agent is done → Intent: CompleteTask or Noop

Each Intent becomes a StagedEffect in the TransactionBuffer and is
committed atomically with other effects in Phase 7 (Commit).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class IntentType(str, Enum):
    """Types of intents an agent can produce."""

    NOOP = "noop"
    SUBMIT_LLM_REQUEST = "submit_llm_request"
    SUBMIT_TOOL_REQUEST = "submit_tool_request"
    SEND_EMAIL = "send_email"
    DELEGATE = "delegate"
    WRITE_PRIVATE_FILE = "write_private_file"
    WAIT_FOR_EVENT = "wait_for_event"
    ACCEPT_TASK = "accept_task"      # T12a: human worker accepts an assignment
    COMPLETE_TASK = "complete_task"
    FAIL_TASK = "fail_task"
    # N4-2 召回引擎 intent
    MEMORY_RECALL = "memory_recall"           # 主动回忆（临时召回策略，延迟 1 tick 生效）
    MEMORY_RECALL_CONFIG = "memory_recall_config"  # 更新可控查询词（持久影响召回）


class Intent(BaseModel):
    """A single, non-blocking step in an agent's ReAct continuation.

    This is the primary output of agent decide(). It replaces ActionPlan
    as the unit of agent decision-making. Each activation produces 0 or
    more Intents, each representing a finite advancement.
    """

    intent_id: str = Field(
        default_factory=lambda: f"intent.{uuid.uuid4().hex[:12]}",
        description="Unique intent identifier",
    )
    intent_type: IntentType = Field(description="Type of intent")
    agent_id: str = Field(description="Agent producing this intent")
    task_id: str = Field(default="", description="Associated task ID")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Intent-specific payload",
    )


class SubmitLLMRequest(Intent):
    """Intent: submit an LLM request to the provider.

    The agent does NOT wait for the response in this activation.
    Instead, the system registers the request, the agent transitions
    to WAITING_FOR_LLM, and the response arrives as an LLM_RESULT
    WakeEvent in a future tick.
    """

    intent_type: IntentType = Field(
        default=IntentType.SUBMIT_LLM_REQUEST,
        init=False,
    )
    request_id: str = Field(
        default_factory=lambda: f"llm.req.{uuid.uuid4().hex[:8]}",
        description="LLM request ID for correlation",
    )
    model: str = Field(default="", description="Model to use")
    messages: tuple[dict[str, str], ...] = Field(
        default_factory=tuple,
        description="Messages for the LLM",
    )
    tools: tuple[dict[str, Any], ...] = Field(
        default_factory=tuple,
        description="Tool definitions for function calling",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=128000)
    timeout_ticks: int = Field(
        default=10,
        ge=1,
        description="Ticks before request is considered timed out",
    )


class SubmitToolRequest(Intent):
    """Intent: submit a tool invocation request.

    Similar to LLM: the agent does NOT wait for the tool result.
    The system registers the request, the agent transitions to
    WAITING_FOR_TOOL, and the result arrives as a TOOL_RESULT
    WakeEvent in a future tick.
    """

    intent_type: IntentType = Field(
        default=IntentType.SUBMIT_TOOL_REQUEST,
        init=False,
    )
    request_id: str = Field(
        default_factory=lambda: f"tool.req.{uuid.uuid4().hex[:8]}",
        description="Tool request ID for correlation",
    )
    tool_name: str = Field(description="Name of the tool to invoke")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments",
    )
    timeout_ticks: int = Field(
        default=5,
        ge=1,
        description="Ticks before request is considered timed out",
    )


class SendEmailIntent(Intent):
    """Intent: send an email to another agent."""

    intent_type: IntentType = Field(
        default=IntentType.SEND_EMAIL,
        init=False,
    )
    to: list[str] = Field(description="Recipient agent IDs")
    subject: str = Field(description="Email subject")
    body: str = Field(default="", description="Email body")
    # v0.10 T8b: structured attachment references ({ref_type, path, version,
    # hash, size, mime}), carried on the email (not copied).
    attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Attachment references",
    )
    email_type: str = Field(default="progress", description="Email type")
    task_id: str = Field(default="", description="Associated task ID")


class DelegateIntent(Intent):
    """Intent: delegate a task to a child agent."""

    intent_type: IntentType = Field(
        default=IntentType.DELEGATE,
        init=False,
    )
    recipient_agent_id: str = Field(description="Target child agent")
    task_title: str = Field(description="Task title")
    task_description: str = Field(default="", description="Task description")
    derived_from: str = Field(default="", description="Task this delegation derives from")
    deadline: datetime | None = Field(
        default=None,
        description=(
            "Task deadline (real-calendar time, SPEC §9.1 — no tick fields "
            "in the business layer)"
        ),
    )
    skill: str | None = Field(
        default=None,
        description=(
            "Skill tag for pool routing (skill_match strategy) when the "
            "recipient is a WorkerPool service manager (SPEC §9.3)"
        ),
    )


class WritePrivateFileIntent(Intent):
    """Intent: write a file to the agent's private workspace."""

    intent_type: IntentType = Field(
        default=IntentType.WRITE_PRIVATE_FILE,
        init=False,
    )
    path: str = Field(description="Relative path in private workspace")
    content: str = Field(description="File content")


class WaitForEventIntent(Intent):
    """Intent: agent explicitly waits for a specific event.

    The agent transitions to the appropriate WAITING_FOR_* state
    and will not be scheduled until the matching event arrives.
    """

    intent_type: IntentType = Field(
        default=IntentType.WAIT_FOR_EVENT,
        init=False,
    )
    waiting_state: str = Field(
        description="Which WAITING_FOR_* state to enter",
    )
    event_types: list[str] = Field(
        default_factory=list,
        description="Event types that will wake the agent",
    )
    task_ids: list[str] = Field(
        default_factory=list,
        description="Specific task IDs to match",
    )


class AcceptTaskIntent(Intent):
    """Intent: a human worker accepts an assigned task (T12a).

    The human worker's UI action is translated to this Intent and goes
    through the SAME transaction path as an AI worker's intents
    (Validate → Act → Commit) — no separate channel.
    """

    intent_type: IntentType = Field(
        default=IntentType.ACCEPT_TASK,
        init=False,
    )
    task_id: str = Field(description="Task to accept")


class CompleteTaskIntent(Intent):
    """Intent: mark the current task as completed."""

    intent_type: IntentType = Field(
        default=IntentType.COMPLETE_TASK,
        init=False,
    )
    task_id: str = Field(description="Task to complete")
    summary: str = Field(default="", description="Completion summary")
    artifacts: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Output artifacts",
    )


class FailTaskIntent(Intent):
    """Intent: mark the current task as failed."""

    intent_type: IntentType = Field(
        default=IntentType.FAIL_TASK,
        init=False,
    )
    task_id: str = Field(description="Task to fail")
    reason: str = Field(description="Failure reason")
    retryable: bool = Field(default=False, description="Can be retried")


class MemoryRecallIntent(Intent):
    """Intent: 主动回忆（memory_recall，N4-2）。

    agent 请求对指定关键词执行一次临时召回；结果写入
    recall_config.temp_overrides，**延迟 1 tick 生效**——因为
    Act 在 Observe 之后，本 tick 的注入集已确定，下 tick 的
    Observe 阶段才会消费 temp_overrides（结构性延迟，非额外等待）。

    实现路径：
    - 走 Effect（MEMORY_RECALL），非 pending op；
    - 无外部副作用，不需要 op 生命周期；
    - 框架在 Act → Commit 后把 temp_overrides 写入 RecallConfig；
    - 下 tick 召回时优先合并 temp_overrides（消费后自动清空）。
    """

    intent_type: IntentType = Field(
        default=IntentType.MEMORY_RECALL,
        init=False,
    )
    # 本次主动回忆的临时覆盖词列表（补充进当前 tick 的查询词空间）
    temp_query_terms: list[str] = Field(
        description="临时召回词列表（一次性，下 tick 消费后清空）",
    )
    # 可选：限制召回类型
    recall_types: list[str] = Field(
        default_factory=list,
        description="限制召回的 MemoryEntryType（空=不限制）",
    )


class MemoryRecallConfigIntent(Intent):
    """Intent: 更新可控查询词（memory_recall_config，N4-2）。

    agent 可显式控制可控查询词（persistent_query_terms），持久影响
    每 tick 的触发召回。

    效果：MEMORY_RECALL_CONFIG effect 写入 RecallConfig.persistent_query_terms。
    逆操作：恢复更新前的词列表（INVERT_CONTRACT 已注册）。
    """

    intent_type: IntentType = Field(
        default=IntentType.MEMORY_RECALL_CONFIG,
        init=False,
    )
    # 新的可控查询词列表（完全替换）
    persistent_query_terms: list[str] = Field(
        description="新的可控查询词列表（持久，完全替换旧值）",
    )
