"""Agent runtime interface: ToolContext, AgentRuntime protocol, and tool registry.

Per SPEC §8.2 (Phases 3-5), §10, §15.1:
- ToolContext binds agent identity to every tool call
- AgentRuntime defines observe/decide/act protocol

v0.11（N1b，SPEC §5.1）：独立工具白名单已废除——按 role 的工具
常量与按名字的 ``agent.tools`` 不再存在；
权限求值一律经 Authority 两层 Grant（∃position：Grant(agent,
position) ∧ Grant(position, entity_id)，§3.5）。``ToolRegistry`` 保留
为 manifest/handler 注册表与授权求值桥：接入 Simulation 的注册表带
``Authority`` 引用，求值走两层 Grant；``ToolContext.allowed_tools``
字段保留仅作兼容（决策路径不再读取）。

v0.6.0: decide() produces list[Intent] — finite, non-blocking steps in
the agent's ReAct continuation. ActionPlan remains as the internal
parsing result of LLM responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from my_team.consolidation import MEMORY_TOOL_NAMES
from my_team.devices.authority import Authority
from my_team.devices.base import Device, EntityKind, RegisteredEntity
from my_team.models.continuation import ContinuationPhase
from my_team.models.intent import (
    AcceptTaskIntent,
    CompleteTaskIntent,
    DelegateIntent,
    FailTaskIntent,
    Intent,
    SendEmailIntent,
    SubmitToolRequest,
    WritePrivateFileIntent,
)
from my_team.tool_manifest import (
    OperationPolicy,
    PolicyDecision,
    ToolManifest,
    ToolManifestError,
)

# ---------------------------------------------------------------------------
# ToolContext — identity binding for every tool call
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolContext:
    """Immutable context绑定到每次工具调用。

    系统侧从 context 获取 agent_id，忽略调用者提交的身份字段。
    这是防止身份伪造的核心机制 (SPEC §15.1, §18.10)。
    """

    agent_id: str
    simulation_id: str = ""
    tick: int = 0
    # DEPRECATED（N1b，§5.1）：白名单载体已废除，权限求值一律经
    # Authority 两层 Grant（§3.5）；本字段保留仅为兼容存量构造/读取
    # （Simulation 构造的 context 不再携带）。
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    # Frozen per-agent file view captured at Freeze (v0.6.0 hardening):
    # {"files": {relpath: content}, "dirs": [relpaths]}. Read-only tools
    # execute against this view instead of the live filesystem.
    read_view: dict[str, Any] | None = field(default=None)
    # Pending-op correlation id (v0.8.0 P2-10): set when a dispatched
    # op runs an in-process executor — the handler registers its live
    # subprocess under this id so cancel_operation can kill it.
    request_id: str = field(default="")
    # Subsystem handles injected by the kernel for PLUGIN handlers
    # (v0.10 T7): the ONLY way a plugin reaches subsystems (file / KB /
    # mail / task tree / ...). Plugin handlers must never touch
    # Simulation internals; handles is a read-only mapping provided at
    # registration time.
    handles: Mapping[str, Any] = field(default_factory=dict)


class ToolPermissionError(Exception):
    """Raised when an agent attempts to use a tool it doesn't have."""

    def __init__(self, agent_id: str, tool_name: str) -> None:
        self.agent_id = agent_id
        self.tool_name = tool_name
        super().__init__(
            f"Agent '{agent_id}' does not have permission to use tool '{tool_name}'"
        )


class ToolResult(BaseModel):
    """Standardized result from a tool execution."""

    success: bool = Field(description="Whether the tool call succeeded")
    data: Any = Field(default=None, description="Tool output data")
    error: str | None = Field(default=None, description="Error message if failed")
    error_code: str | None = Field(
        default=None,
        description="Machine-readable error code: permission_denied, tool_error, not_found, etc.",
    )
    retryable: bool = Field(
        default=True,
        description="Whether this failure is retryable (permission_denied is not)",
    )
    agent_id: str = Field(default="", description="Agent that called the tool")
    tool_name: str = Field(default="", description="Tool that was called")
    tick: int = Field(default=0, description="Tick at which tool was called")


# ---------------------------------------------------------------------------
# Tool registry — manifest/handler registry + Authority authorization bridge
# ---------------------------------------------------------------------------

class ToolCategory(str, Enum):
    """Categories of tools for permission grouping.

    DEPRECATED（N1b）：权限分组不再按名字类别——有效权限 = 两层 Grant
    （∃position：Grant(agent, position) ∧ Grant(position, entity_id)，
    §3.5/§5.1）。保留枚举值仅防存量字符串引用断裂。
    """

    FILE_OPS = "file_ops"           # read, write, ls on private space
    SHARED_KB = "shared_kb"         # read, write on shared knowledge base
    EMAIL = "email"                 # send_email
    DELEGATE = "delegate"           # delegate to children
    SYSTEM = "system"               # system-level operations


class _ToolEntityDevice(Device):
    """工具受控实体载体（N1b，§5.1）：每个注册工具 = 一个 TOOL uuid。

    manifest 的 ``capability`` 即受控 uuid；经 ``accept_device`` 提交到
    Authority 注册中心（设备注册 = 向 Authority 注册工具 uuid）。
    """

    def __init__(self, device_id: str) -> None:
        super().__init__(device_id)

    def declare(self, entity: RegisteredEntity) -> None:
        self._entities[entity.entity_id] = entity


class ToolRegistry:
    """工具注册表：manifest + handler 注册 + 授权求值桥（N1b）。

    v0.11（N1b，§5.1）起**废除独立白名单**（按 role 的工具常量与按名字
    的 ``agent.tools`` 已删除）：权限求值一律经 Authority 两层 Grant
    （§3.5），本注册表只维护 manifest/handler 与「工具名 → capability
    uuid」映射。

    - ``authority`` 为 None 的**裸注册表**（存量测试/独立使用）：退回
      旧式 ``_agent_tools`` 记录，仅作兼容；
    - 接入 Simulation 的注册表**必带 authority**：``register_agent`` /
      ``get_allowed_tools`` 仅为弃用桥（Add 语义授予/读回授权集），
      ``execute`` 的授权判定走 Authority（deny-by-default）。

    初始授予集（引导布线）：``declare_tools`` 记录 agent 声明的工具集
    合并即时授予已注册实体；**迟到注册补授**——manifest 在布线之后注册
    （如集成/插件工具）时，对声明过该工具的 agent 自动授予（保持存量
    e2e 行为；未声明者永不自动授予）。
    """

    def __init__(
        self,
        authority: Authority | None = None,
        phase_provider: Callable[[str], "ContinuationPhase | None"] | None = None,
    ) -> None:
        self._authority = authority
        # N4-4：相位提供器（agent_id → 当前 ContinuationPhase，None=未知）。
        # CONSOLIDATING 相位下授权集动态收窄为记忆工具集（工具面收窄，
        # SPEC §4.4 —— 授权集本就是动态求值，零新机制）。
        self._phase_provider = phase_provider
        # 兼容回退（无 authority 的裸注册表）：agent_id → 工具名集合。
        self._agent_tools: dict[str, frozenset[str]] = {}
        self._tool_handlers: dict[str, Any] = {}
        self._manifests: dict[str, ToolManifest] = {}
        self._policy: OperationPolicy | None = None
        # 工具名 → capability uuid（manifest 注册时填充，§5.1 受控 uuid）。
        self._tool_entities: dict[str, str] = {}
        # 初始授予集声明（agent_id → 声明的工具名集合，N1b 引导布线）。
        self._declared_tools: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # 初始授予集（N1b 引导布线：org 初始化 + 初始授予集，§5.1）
    # ------------------------------------------------------------------

    def declare_tools(self, agent_id: str, tools: frozenset[str]) -> None:
        """声明 agent 的初始授予集（Add 语义，永不撤销）。

        立即授予已注册实体的工具；未注册实体（如集成/插件工具）在
        manifest 注册时补授（``_grant_deferred``）。deny-by-default：
        未注册 uuid 的工具不可授予（§3.5）。
        """
        self._declared_tools.setdefault(agent_id, set()).update(tools)
        if self._authority is None:
            # 裸注册表兼容回退：记录旧式白名单集合。
            self._agent_tools[agent_id] = frozenset(tools)
            return
        for tool in tools:
            entity_id = self._tool_entities.get(tool)
            if entity_id is not None:
                self._authority.grant_membership(agent_id, agent_id)
                self._authority.grant_capability(agent_id, entity_id)

    def register_agent(self, agent_id: str, tools: frozenset[str]) -> None:
        """DEPRECATED（N1b，§5.1）：白名单载体已废除。

        仅为兼容存量调用：等价于把 ``tools`` 并入该 agent 的初始授予集
        （Add 语义——授予永不因重复声明撤销）。
        """
        self.declare_tools(agent_id, tools)

    def get_allowed_tools(self, agent_id: str) -> frozenset[str]:
        """DEPRECATED（N1b）：白名单 API 兼容桥，等价 ``authorized_tools``。"""
        return self.authorized_tools(agent_id)

    def authorized_tools(self, agent_id: str) -> frozenset[str]:
        """agent 当前被授权（两层 Grant，§3.5）的工具名集合。

        deny-by-default：未注册实体 / 无授予的工具不出现。

        N4-4（工具面收窄）：CONSOLIDATING 相位下授权集切换为记忆工具集
        （SPEC §4.4）——LLM 只见记忆工具定义，杜绝整理期间调用业务工具。
        """
        if self._authority is not None:
            granted = frozenset(
                name
                for name, entity_id in self._tool_entities.items()
                if self._authority.authorize(agent_id, entity_id).allowed
            )
        else:
            granted = self._agent_tools.get(agent_id, frozenset())
        if self._consolidation_restricts(agent_id):
            return granted & MEMORY_TOOL_NAMES
        return granted

    def _consolidation_restricts(self, agent_id: str) -> bool:
        """是否处于 CONSOLIDATING 相位（授权集收窄为记忆工具集）。"""
        if self._phase_provider is None:
            return False
        return self._phase_provider(agent_id) == ContinuationPhase.CONSOLIDATING

    def capability_for(self, tool_name: str) -> str | None:
        """工具名 → 受控 capability uuid（未注册返回 None，§5.1）。"""
        return self._tool_entities.get(tool_name)

    # ------------------------------------------------------------------
    # 注册：manifest / handler / 工具（含 Authority 注册中心提交）
    # ------------------------------------------------------------------

    def _register_entity(self, manifest: ToolManifest) -> None:
        """把 manifest 的 capability 作为受控 uuid 注册进 Authority（§5.1）。

        设备注册 = 向 Authority 注册工具 uuid；注册后对声明过该工具的
        agent 补授（迟到注册补授）。
        """
        if manifest.name in self._tool_entities:
            return  # 已声明（防重复注册实体）
        self._tool_entities[manifest.name] = manifest.capability
        if self._authority is not None:
            device = _ToolEntityDevice(
                manifest.device_id or f"tool:{manifest.name}"
            )
            device.declare(RegisteredEntity(
                entity_id=manifest.capability,
                device_id=device.device_id,
                kind=EntityKind.TOOL,
                label=manifest.name,
            ))
            self._authority.accept_device(device)
        self._grant_deferred(manifest.name)

    def _grant_deferred(self, tool_name: str) -> None:
        """迟到注册补授：布线后注册的 manifest，对声明过它的 agent 授予。

        保持存量 e2e 行为（配置声明 = 初始授予集的来源）；未声明的
        agent 永不自动授予（deny-by-default，§3.5）。
        """
        if self._authority is None:
            return
        entity_id = self._tool_entities.get(tool_name)
        if entity_id is None:
            return
        for agent_id, tools in self._declared_tools.items():
            if tool_name in tools:
                self._authority.grant_membership(agent_id, agent_id)
                self._authority.grant_capability(agent_id, entity_id)

    def register_manifest(self, manifest: ToolManifest) -> None:
        """Register a tool manifest (registration-time validation).

        Raises ToolManifestError if the manifest is invalid.
        """
        self._manifests[manifest.name] = manifest
        self._register_entity(manifest)

    def register_tool(
        self,
        manifest: ToolManifest,
        handler: Any,
    ) -> None:
        """Public plugin API: register a manifest + handler as one unit.

        Registration enforces (v0.10 T7):
        - manifest validity (ToolManifestError on invalid contract)
        - name uniqueness (ToolManifestError on duplicate registration)

        N1b（§5.1）：注册即把工具 capability 作为受控 uuid 提交 Authority
        注册中心；授权判定（能不能用）由 Authority 布线中心的两层 Grant
        求值，设备/注册表不自行判权。

        Policy is NOT touched: deny-by-default applies — a tool is only
        usable while the deployment policy (if any) allowlists it.
        """
        if (
            manifest.name in self._tool_handlers
            or manifest.name in self._manifests
        ):
            raise ToolManifestError(
                f"Tool '{manifest.name}' is already registered"
            )
        self.register_handler(manifest.name, handler, manifest=manifest)

    def register_handler(
        self,
        tool_name: str,
        handler: Any,
        manifest: ToolManifest | None = None,
    ) -> None:
        """Register a callable handler for a tool, optionally with its
        manifest (validated at registration)."""
        if manifest is not None:
            if manifest.name != tool_name:
                raise ValueError(
                    f"Manifest name '{manifest.name}' does not match "
                    f"tool '{tool_name}'"
                )
            self.register_manifest(manifest)
        self._tool_handlers[tool_name] = handler

    def get_manifest(self, tool_name: str) -> ToolManifest | None:
        """Get the manifest for a tool (None if not registered)."""
        return self._manifests.get(tool_name)

    def manifests(self) -> tuple[ToolManifest, ...]:
        """All registered manifests, sorted by tool name."""
        return tuple(
            self._manifests[name] for name in sorted(self._manifests)
        )

    def set_policy(self, policy: OperationPolicy) -> None:
        """Attach a deployment policy (deny-by-default)."""
        self._policy = policy

    @property
    def policy(self) -> OperationPolicy | None:
        return self._policy

    def policy_decision(self, tool_name: str) -> PolicyDecision:
        """Check a tool against the attached policy.

        Without a policy: always allowed. With a policy: a tool without
        a manifest cannot be policy-checked → denied (deny-by-default).
        """
        if self._policy is None:
            return PolicyDecision(True, False, "")
        manifest = self._manifests.get(tool_name)
        if manifest is None:
            return PolicyDecision(
                False, False,
                f"Tool '{tool_name}' has no manifest; cannot policy-check",
            )
        return self._policy.decide(manifest)

    # ------------------------------------------------------------------
    # 授权求值：两层 Grant（∃position：Grant ∧ Grant ∧ 锁，§3.5/§5.1）
    # ------------------------------------------------------------------

    def _authorized(self, context: ToolContext, tool_name: str) -> bool:
        """两层 Grant 求值（锁约束由内核 handler 路径叠加，见 handler 实现）。

        带 authority：工具有受控 uuid → ``authorize(agent_id, uuid)``；
        无 uuid（manifest 缺失）→ 兼容回退 context.allowed_tools
        （Simulation 构造的 context 不含该字段 → 生产路径 deny-by-default）。
        裸注册表：旧式 ``_agent_tools`` 记录。

        N4-4（工具面收窄）：CONSOLIDATING 相位下只允许记忆工具集，
        其余工具一律拒绝（执行侧防绕行，与 authorized_tools 渲染侧一致）。
        """
        if self._consolidation_restricts(context.agent_id):
            if tool_name not in MEMORY_TOOL_NAMES:
                return False
        if self._authority is not None:
            entity_id = self._tool_entities.get(tool_name)
            if entity_id is not None:
                return self._authority.authorize(
                    context.agent_id, entity_id
                ).allowed
            return tool_name in context.allowed_tools
        return tool_name in self._agent_tools.get(context.agent_id, frozenset())

    def authorize(self, context: ToolContext, tool_name: str) -> None:
        """Verify the agent has permission to use the tool.

        Raises ToolPermissionError if not authorized.
        """
        if not self._authorized(context, tool_name):
            raise ToolPermissionError(context.agent_id, tool_name)

    def can_use(self, agent_id: str, tool_name: str) -> bool:
        """Check if an agent can use a tool (non-raising)."""
        if self._authority is not None:
            entity_id = self._tool_entities.get(tool_name)
            if entity_id is None:
                return False
            return self._authority.authorize(agent_id, entity_id).allowed
        return tool_name in self._agent_tools.get(agent_id, frozenset())

    def execute(
        self,
        context: ToolContext,
        tool_name: str,
        **kwargs: Any,
    ) -> ToolResult:
        """Authorize and execute a tool call.

        1. Verify agent has permission (two-layer Grant via Authority)
        2. Look up handler
        3. Execute with context
        """
        try:
            self.authorize(context, tool_name)
        except ToolPermissionError as e:
            return ToolResult(
                success=False,
                error=str(e),
                error_code="permission_denied",
                retryable=False,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )

        # Policy gate (v0.7.0): deny-by-default when a policy is attached.
        decision = self.policy_decision(tool_name)
        if not decision.allowed:
            return ToolResult(
                success=False,
                error=decision.reason,
                error_code="policy_denied",
                retryable=False,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )
        if decision.requires_approval:
            return ToolResult(
                success=False,
                error=decision.reason,
                error_code="requires_approval",
                retryable=False,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )

        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                success=False,
                error=f"No handler registered for tool '{tool_name}'",
                error_code="not_found",
                retryable=False,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )

        try:
            result = handler(context=context, **kwargs)
            if isinstance(result, ToolResult):
                return result
            return ToolResult(
                success=True,
                data=result,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                error_code="tool_error",
                retryable=True,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )


# ---------------------------------------------------------------------------
# Agent observations and action plans
# ---------------------------------------------------------------------------

class AgentObservation(BaseModel):
    """What an agent sees during the Observe phase (SPEC §8.2 Phase 3)."""

    agent_id: str = Field(description="Agent receiving this observation")
    tick: int = Field(description="Current simulation tick")
    emails: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Emails delivered to this agent's inbox",
    )
    task_states: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="States of tasks owned by this agent",
    )
    shared_kb_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="Relevant shared KB state",
    )
    lock_states: dict[str, Any] = Field(
        default_factory=dict,
        description="Lock states relevant to this agent",
    )
    private_workspace_path: str = Field(
        default="",
        description="Path to agent's private workspace",
    )
    pending_human_actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "T12a: pending human UI actions (accept/complete/fail) routed "
            "to this kind=human agent, awaiting translation to Intents"
        ),
    )
    # N4-3 注入组装器：本轮注入布局元数据（审计复盘，非内容快照）
    memory_injection: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "N4-3 注入布局版本戳：{layout_refs, detail_levels, stamp_hash, "
            "pending_consolidation}；空字典 = 本轮无注入（兼容旧代码）"
        ),
    )


def _proxy(d: dict[str, Any]) -> MappingProxyType[str, Any]:
    """Wrap a dict in MappingProxyType for deep immutability."""
    return MappingProxyType(d)


def _proxy_nested(
    d: dict[str, dict[str, Any]],
) -> MappingProxyType[str, MappingProxyType[str, Any]]:
    """Wrap a nested dict in MappingProxyType for deep immutability."""
    return MappingProxyType({k: MappingProxyType(v) for k, v in d.items()})


@dataclass(frozen=True)
class AgentSnapshot:
    """Immutable per-agent view of the simulation state at tick boundary.

    Constructed by the simulation during Phase 3 (Observe) and passed
    to each agent's observe() method. Agents should read from this
    snapshot and produce an AgentObservation.

    All fields are deeply immutable:
    - Frozen dataclass prevents field reassignment
    - emails is a tuple of dicts (tuple is immutable)
    - task_states, shared_kb_snapshot, lock_states use MappingProxyType
      to prevent mutation of nested values
    """

    tick: int = 0
    emails: tuple[dict[str, Any], ...] = ()
    task_states: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    shared_kb_snapshot: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    lock_states: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    private_workspace_path: str = ""

    def __post_init__(self) -> None:
        """Wrap mutable fields in MappingProxyType for deep immutability."""
        # Frozen dataclass allows mutation in __post_init__ via object.__setattr__
        if not isinstance(self.task_states, MappingProxyType):
            object.__setattr__(
                self, "task_states",
                MappingProxyType({k: MappingProxyType(v) for k, v in self.task_states.items()})
            )
        if not isinstance(self.shared_kb_snapshot, MappingProxyType):
            object.__setattr__(
                self, "shared_kb_snapshot", MappingProxyType(self.shared_kb_snapshot)
            )
        if not isinstance(self.lock_states, MappingProxyType):
            object.__setattr__(
                self, "lock_states", MappingProxyType(self.lock_states)
            )


class AgentAction(BaseModel):
    """A single action produced by an agent during Decide (SPEC §8.2 Phase 4)."""

    action_type: str = Field(description="Action type: send_email, read, write, delegate, etc.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Action parameters")
    tool_name: str = Field(default="", description="Tool to use for this action")


class ActionPlan(BaseModel):
    """An agent's planned actions for the current tick."""

    agent_id: str = Field(description="Agent that produced this plan")
    tick: int = Field(description="Tick this plan is for")
    actions: list[AgentAction] = Field(default_factory=list, description="Planned actions")


class ActionResult(BaseModel):
    """Result of executing a single action."""

    action: AgentAction = Field(description="The action that was executed")
    success: bool = Field(description="Whether the action succeeded")
    result_data: Any = Field(default=None, description="Action output")
    error: str | None = Field(default=None, description="Error if failed")
    # Structured validation error code (v0.7.0 review): CAPABILITY_DENIED,
    # TOOL_MANIFEST_MISSING, POLICY_DENIED, APPROVAL_REQUIRED,
    # BUDGET_EXCEEDED, DUPLICATE_REQUEST_ID, TASK_NOT_FOUND,
    # DEADLINE_EXCEEDED, INVALID_ARGUMENT, patch_conflict, ...
    error_code: str | None = Field(
        default=None,
        description="Machine-readable error code",
    )


def action_plan_to_intents(plan: ActionPlan) -> list[Intent]:
    """Convert a legacy ActionPlan into a list of Intents.

    This bridges the old rule-based agent interface (which produces
    AgentAction lists) to the v0.6.0 Intent model. Rule-based agents
    (BaseAgent subclasses) can keep producing ActionPlans; the system
    converts them to Intents before staging.

    Mapping:
      - delegate   → DelegateIntent
      - send_email → SendEmailIntent
      - write      → WritePrivateFileIntent
      - read/ls/kb_read/kb_list/kb_search/kb_write/kb_create
                   → SubmitToolRequest
    """
    intents: list[Intent] = []
    for action in plan.actions:
        payload = action.payload or {}
        if action.action_type == "delegate":
            intents.append(DelegateIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                recipient_agent_id=payload.get("recipient_agent_id", ""),
                task_title=payload.get("task_title", ""),
                task_description=payload.get("task_description", ""),
                derived_from=payload.get("derived_from", ""),
                deadline=payload.get("deadline"),
            ))
        elif action.action_type == "send_email":
            intents.append(SendEmailIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                to=list(payload.get("to", [])),
                subject=payload.get("subject", ""),
                body=payload.get("body", ""),
                email_type=payload.get("email_type", "progress"),
                attachments=list(payload.get("attachments", [])),  # T8b
            ))
        elif action.action_type == "write":
            intents.append(WritePrivateFileIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                path=payload.get("path", ""),
                content=payload.get("content", ""),
            ))
        elif action.action_type in {
            "read", "ls",
            "kb_read", "kb_list", "kb_search",  # v0.10 T8a
            "kb_write", "kb_create",
            # N4-4 记忆工具集（CONSOLIDATING 整理动作 → SubmitToolRequest）
            *MEMORY_TOOL_NAMES,
        }:
            intents.append(SubmitToolRequest(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                tool_name=action.tool_name or action.action_type,
                arguments=payload,
            ))
        elif action.action_type == "complete_task":
            intents.append(CompleteTaskIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                summary=payload.get("summary", ""),
                artifacts=payload.get("artifacts", []),
            ))
        elif action.action_type == "fail_task":
            intents.append(FailTaskIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                reason=payload.get("reason", ""),
                retryable=payload.get("retryable", False),
            ))
    return intents


class ActionContext(BaseModel):
    """Context for action execution (SPEC §8.2 Phase 5)."""

    agent_id: str
    tick: int
    tool_context: ToolContext = Field(description="Identity-bound context for tool calls")


# ---------------------------------------------------------------------------
# AgentRuntime protocol — what a real agent implementation must provide
# ---------------------------------------------------------------------------

@runtime_checkable
class AgentRuntime(Protocol):
    """Protocol that all agent implementations must satisfy.

    This defines the three-phase interface for agent execution within
    a simulation tick:

    1. observe: Read the frozen snapshot and produce observations
    2. decide: Generate an action plan based on observations
    3. act: Execute the planned actions
    """

    @property
    def agent_id(self) -> str:
        """Unique identifier for this agent."""
        ...

    def observe(self, snapshot: AgentSnapshot) -> AgentObservation:
        """Phase 3 (Observe): Read the frozen snapshot.

        The agent reads:
        - New emails in inbox
        - Current task states
        - Private workspace state
        - Shared KB snapshot
        - Lock states
        - System notifications

        Returns an AgentObservation with relevant data.
        """
        ...

    def decide(self, observation: AgentObservation) -> ActionPlan:
        """Phase 4 (Decide): Generate an action plan (legacy interface).

        Rule-based agents produce an ActionPlan. The system converts it
        to Intents via action_plan_to_intents(). LLM agents should
        override decide_intents() instead — this method is retained for
        backward compatibility with rule-based agents.
        """
        ...

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: Any = None,
    ) -> list[Intent]:
        """Phase 5 (Decide, v0.6.0): Produce non-blocking Intents.

        The agent analyzes its observations and produces 0 or more
        Intents — finite, non-blocking steps in its ReAct continuation.
        No side effects occur during this phase.

        Intents can be:
          - SubmitLLMRequest  (async LLM call, does NOT block)
          - SubmitToolRequest (async tool call)
          - SendEmailIntent / DelegateIntent / WritePrivateFileIntent
          - WaitForEventIntent / CompleteTaskIntent / FailTaskIntent

        The default implementation converts decide()'s ActionPlan.
        """
        ...

    def act(
        self,
        plan: ActionPlan,
        context: ActionContext,
    ) -> list[ActionResult]:
        """Phase 5 (Act): Execute the planned actions.

        Actions produce staged effects that are not visible to other
        agents until the Commit phase.

        Each tool call must go through the ToolRegistry with the
        agent's ToolContext for identity verification.
        """
        ...


# ---------------------------------------------------------------------------
# Built-in agent implementations
# ---------------------------------------------------------------------------

class BaseAgent:
    """Base implementation of AgentRuntime with common functionality.

    Subclass this and override decide() to implement custom agent logic.
    """

    def __init__(
        self,
        agent_id: str,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._tool_registry = tool_registry or ToolRegistry()
        # N1b（§5.1）：权限求值一律经 Authority 两层 Grant，context 不再
        # 携带 allowed_tools（字段保留仅作兼容，默认空集）。
        self._tool_context = ToolContext(agent_id=agent_id)

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def tool_context(self) -> ToolContext:
        return self._tool_context

    def observe(self, snapshot: AgentSnapshot) -> AgentObservation:
        """Default observe: extract relevant data from snapshot."""
        # Convert MappingProxyType back to plain dicts for AgentObservation
        task_states = {
            k: dict(v) for k, v in snapshot.task_states.items()
        }
        return AgentObservation(
            agent_id=self._agent_id,
            tick=snapshot.tick,
            emails=list(snapshot.emails),
            task_states=task_states,
            shared_kb_snapshot=dict(snapshot.shared_kb_snapshot),
            lock_states=dict(snapshot.lock_states),
            private_workspace_path=snapshot.private_workspace_path,
        )

    def decide(self, observation: AgentObservation) -> ActionPlan:
        """Default decide: no actions (override in subclass)."""
        return ActionPlan(
            agent_id=self._agent_id,
            tick=observation.tick,
            actions=[],
        )

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: Any = None,
    ) -> list[Intent]:
        """Default decide_intents: convert decide()'s ActionPlan to Intents.

        Rule-based agents keep overriding decide() to produce
        ActionPlans; the system converts them to Intents automatically.
        LLM agents override this method directly to produce
        SubmitLLMRequest intents.
        """
        plan = self.decide(observation)
        return action_plan_to_intents(plan)

    def act(
        self,
        plan: ActionPlan,
        context: ActionContext,
    ) -> list[ActionResult]:
        """Default act: execute actions through tool registry."""
        results: list[ActionResult] = []
        for action in plan.actions:
            try:
                tool_context = ToolContext(
                    agent_id=self._agent_id,
                    tick=context.tick,
                )
                result = self._tool_registry.execute(
                    context=tool_context,
                    tool_name=action.tool_name,
                    **action.payload,
                )
                results.append(ActionResult(
                    action=action,
                    success=result.success,
                    result_data=result.data,
                    error=result.error,
                ))
            except ToolPermissionError as e:
                results.append(ActionResult(
                    action=action,
                    success=False,
                    error=str(e),
                ))
        return results


class RootAgent(BaseAgent):
    """Root decision agent (SPEC §4.2).

    N1b（§5.1）：工具权限不再按 role 内置（旧工具常量已废除）——有效
    权限 = 两层 Grant（§3.5），由 Simulation 初始布线/场景包授予决定；
    本类仅保留角色语义（决策入口/调度），不再携带任何工具集合。
    """

    def __init__(self, agent_id: str = "agent.root", **kwargs: Any) -> None:
        super().__init__(agent_id=agent_id, **kwargs)


class SubAgent(BaseAgent):
    """Sub-agent (SPEC §4.3) with role semantics only.

    N1b（§5.1）：业务工具的可用性由两层 Grant 决定（§3.5），不再按
    role/extra_tools 内置白名单（旧工具常量已废除）。
    """


class ManagerAgent(BaseAgent):
    """Manager agent that can delegate (e.g., Research Agent).

    N1b（§5.1）：delegate/send_email 等能力经 Authority 授予（§3.5），
    不再按 role 内置（旧工具常量已废除）；本类仅保留角色语义。
    """


class HumanWorkerRuntime(BaseAgent):
    """Runtime for a ``kind=human`` agent (T12a, SPEC §10.1).

    A human worker is UI-queue driven — NOT LLM driven. It never emits
    LLM/tool requests; it only translates pending human UI actions
    (accept / complete / fail) into the SAME transaction-path Intents
    an AI worker would produce (Validate → Act → Commit), so human and
    AI workers share one channel.

    The pending actions arrive via the observation
    (``pending_human_actions``), which the simulation fills from the
    IngressBuffer's ``human``-source events (human-action ingress →
    wait/wake path, T9 决策 3).
    """

    def __init__(self, agent_id: str, **kwargs: Any) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        # Humans act through the UI command surface, not tools. Give
        # them an empty tool context so no tool path is usable.
        self._tool_context = ToolContext(
            agent_id=agent_id,
            allowed_tools=frozenset(),
        )

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: Any = None,
    ) -> list[Intent]:
        """Translate pending human UI actions into task Intents.

        Each pending action is a dict shaped like::

            {"action": "accept"|"complete"|"fail",
             "task_id": "...", "summary": "...", "reason": "..."}

        One action → one Intent; all go through the standard transaction
        path. No pending actions → no Intents (the human queue just sits).
        """
        intents: list[Intent] = []
        for action in observation.pending_human_actions:
            task_id = action.get("task_id", "")
            act = action.get("action", "")
            if act == "accept":
                intents.append(AcceptTaskIntent(
                    agent_id=self._agent_id, task_id=task_id,
                ))
            elif act == "complete":
                intents.append(CompleteTaskIntent(
                    agent_id=self._agent_id,
                    task_id=task_id,
                    summary=action.get("summary", ""),
                    artifacts=list(action.get("artifacts", [])),
                ))
            elif act == "fail":
                intents.append(FailTaskIntent(
                    agent_id=self._agent_id,
                    task_id=task_id,
                    reason=action.get("reason", "failed by human"),
                    retryable=bool(action.get("retryable", False)),
                ))
        return intents
