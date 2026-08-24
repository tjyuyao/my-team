"""Agent data model.

v0.11（N2）起提供**新 Agent 模型**（``Agent``，SPEC §4.1：agent_id
uuid4 + kind + position_ref + 运行模式字段；岗人分离，占据即继承）。
旧 ``AgentConfig``（role/tools 白名单/parent-children，SPEC §17 时代）
**保留**以兼容存量代码与测试（字符串 agent_id、parent/children/role
字段），标注弃用；实际拆除留给 N1b/N3 联调。``PoolConfig`` 保留
（kind=service 兼 WorkerPool manager，§7.3）。

v0.11（N1b，§5.1）：``AgentConfig.tools`` **字段已删除**（白名单载体
废除，SPEC §5.1「废除独立工具白名单」）——权限一律走两层 Grant
（§3.5），config 不再声明"可用工具"。为兼容存量读取（``control_plane``
展示、``Simulation._initialize`` 初始授予集来源、存量测试），保留只读
``tools`` 属性桥接 ``model_extra``（``extra="allow"`` 透传）；该值
**不是权限依据**，仅作引导布线的初始授予集来源（N3 场景包可替换）。
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class AgentRole(str, Enum):
    """Predefined agent roles.

    **DEPRECATED（v0.11，N2）**：role 并入 position（ACL 主体 =
    position，§1.8），不再单独设计；保留兼容，拆除留给 N1b/N3 联调。
    """

    ROOT_DECISION = "root_decision_agent"
    RESEARCH_MANAGER = "research_manager"
    WEB_RESEARCHER = "web_researcher"
    DATA_ANALYST = "data_analyst"
    PLANNING = "planning_manager"
    REVIEW = "review_manager"
    QUALITY_CHECK = "quality_check_agent"
    CUSTOM = "custom"


class AgentStatus(str, Enum):
    """Agent runtime states per SPEC §9."""

    CREATED = "created"
    INITIALIZED = "initialized"
    READY = "ready"
    # Running sub-states
    IDLE = "idle"
    PROCESSING = "processing"
    WAITING = "waiting"
    BLOCKED = "blocked"
    PAUSED = "paused"
    FAILED = "failed"
    TERMINATED = "terminated"


class AgentTool(str, Enum):
    """Available agent tools."""

    READ = "read"
    WRITE = "write"
    LS = "ls"
    DELEGATE = "delegate"
    SEND_EMAIL = "send_email"
    WEB_SEARCH = "web_search"


class SharedKBPermission(BaseModel):
    """Permission rule for shared knowledge base access, per SPEC §6.2."""

    scope: str = Field(description="Path pattern, e.g. 'project/research/*'")
    principal: str = Field(description="Agent ID this rule applies to")
    allow: list[str] = Field(
        description="Allowed operations: list, read, create, write, append, "
        "rename, delete, lock, unlock, publish"
    )


class PoolMode(str, Enum):
    """WorkerPool routing behavior (T11 决策 3, SPEC §9.3)."""

    IMMEDIATE = "immediate"  # delegate → select child → copy same tick
    DEFERRED = "deferred"    # queue at manager; dispatch when child idle


class PoolStrategy(str, Enum):
    """Declarative child-selection rules (round_robin needs no LLM)."""

    ROUND_ROBIN = "round_robin"
    LEAST_BUSY = "least_busy"
    SKILL_MATCH = "skill_match"


class PoolConfig(BaseModel):
    """WorkerPool behavior on a ``kind=service`` manager (SPEC §9.3).

    pool = service manager + children + declarative routing rules —
    no independent pool primitive, no pool_id. Selection is executed
    by the kernel as the manager's rule-driven behavior.

    **DEPRECATED（v0.11，N2）**：旧模型字段（见模块 docstring 迁移
    说明）；保留兼容，拆除留给 N1b/N3 联调。
    """

    mode: PoolMode = PoolMode.IMMEDIATE
    strategy: PoolStrategy = PoolStrategy.LEAST_BUSY


class AgentKind(str, Enum):
    """Agent 运行模式（SPEC §4.1：llm | human | service）。

    ``kind`` 决定驱动方式（LLM / 人类 UI 队列 / 服务代理），
    **不是权限依据**（权限以 position 为主体，§1.8/§3.5）。
    """

    LLM = "llm"
    HUMAN = "human"
    SERVICE = "service"


# kind → 该运行模式必须携带的字段（SPEC §4.1）。
_KIND_FIELD: dict[AgentKind, str] = {
    AgentKind.LLM: "llm_profile",
    AgentKind.HUMAN: "human_queue",
    AgentKind.SERVICE: "service_ref",
}


class Agent(BaseModel):
    """v0.11 新 Agent 模型（SPEC §4.1，岗人分离，认知主体）。

    - ``agent_id``：uuid4 全局身份（显示名/标签可读，如经
      ``position_ref`` → Position.name 或 ``metadata``）；
    - ``kind``：运行模式（llm | human | service），**非权限依据**；
    - ``position_ref``：占据的岗位（由组织架构设备定义，§5.8）；
      **占据即继承**其边与授予（解析见
      ``my_team.models.position.effective_capabilities``）——
      ``position_ref=None`` 表示未入岗（直派形态下亦经岗位，§5.8）；
    - ``llm_profile`` / ``human_queue`` / ``service_ref``：按 kind 使用
      （LLM 供应商是 Agent 内部结构，§4.6）；非本 kind 的字段应留空；
    - ``metadata``：附加配置；多版本 agent 候选（同岗不同配置评估）
      预留 ``metadata["variant"]``（N3 mount 挂载用）；
    - **无可持有资产**：经手物（task/report/mail 账号）归属岗位
      （§5.8），不随 agent 身份迁移。

    Design references:
    - SPEC §4.1（Agent 实体）/ §4.6（LLM 执行器）/ §5.8（占据即继承）
    - KANBAN/IN_PROGRESS/2026-08-24-position-model.md（N2）
    """

    agent_id: uuid.UUID
    kind: AgentKind = AgentKind.LLM
    position_ref: uuid.UUID | None = None
    llm_profile: str | None = None
    human_queue: str | None = None
    service_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_id")
    @classmethod
    def _agent_id_must_be_uuid4(cls, v: uuid.UUID) -> uuid.UUID:
        if v.version != 4:
            raise ValueError(f"agent_id must be a uuid4, got {v}")
        return v

    @model_validator(mode="after")
    def _kind_fields_consistent(self) -> Agent:
        """kind 决定驱动方式：本 kind 的字段必填，其余字段留空（§4.1）。"""
        expected = _KIND_FIELD[self.kind]
        actual = {
            "llm_profile": self.llm_profile,
            "human_queue": self.human_queue,
            "service_ref": self.service_ref,
        }
        if actual[expected] is None:
            raise ValueError(
                f"Agent '{self.agent_id}': kind={self.kind.value} 需要 "
                f"{expected}（SPEC §4.1）"
            )
        for name, value in actual.items():
            if name != expected and value is not None:
                raise ValueError(
                    f"Agent '{self.agent_id}': {name} 仅对 "
                    f"kind={_KIND_FIELD[self.kind]} 有效"
                )
        return self


class AgentConfig(BaseModel):
    """Agent configuration as loaded from JSON, per SPEC §17.

    **DEPRECATED（v0.11，N2）**：旧模型（role/tools 白名单/
    parent-children 字段）。迁移目标为 ``Agent``（SPEC §4.1）。本类
    **保留**以兼容存量代码与测试（字符串 agent_id 如 "agent.root"、
    ``parent_id``/``children``/``role`` 字段）；实际拆除留给 N1b/N3
    联调。``PoolConfig`` 保留（kind=service 兼 WorkerPool manager，§7.3）。

    N1b（§5.1）：``tools`` 字段已删除（白名单载体废除）；``role`` 标注
    弃用（ACL 主体 = position，§1.8）。``tools`` 仅经 ``model_extra``
    透传保留（``extra="allow"``），只读属性读取——不是权限依据。

    This is the *static* definition of an agent. Runtime state
    (status, current task, memory) is managed separately.
    """

    # extra="allow"：tools 等白名单时代字段经 model_extra 透传保留
    # （N1b：字段已删，仅兼容读取）。
    model_config = ConfigDict(extra="allow")

    agent_id: str = Field(description="Unique identifier, e.g. 'agent.research'")
    display_name: str = Field(description="Human-readable name")
    role: str = Field(
        description=(
            "**DEPRECATED（N1b）**：role 不是权限依据（ACL 主体 = "
            "position，§1.8/§3.5）；保留仅供 context_compiler/N4 等兼容"
            "读取，实际拆除留给 N3/N4 联调。"
        ),
    )
    kind: Literal["llm", "human", "service"] = Field(
        default="llm",
        description=(
            "Agent kind (SPEC §4.1): llm = LLM-driven; human = UI-queue "
            "driven; service = external-service proxy / rule-driven "
            "(WorkerPool manager, §9.3)"
        ),
    )
    pool: PoolConfig | None = Field(
        default=None,
        description=(
            "WorkerPool routing config; only valid for kind=service "
            "managers with children (T11 决策 3)"
        ),
    )
    parent_id: str | None = Field(
        default=None,
        description="Parent agent ID (null for root)",
    )
    children: list[str] = Field(
        default_factory=list,
        description="Child agent IDs",
    )
    can_delegate: bool = Field(
        default=False,
        description="Whether this agent can delegate to children",
    )
    shared_kb_permissions: list[SharedKBPermission] = Field(
        default_factory=list,
        description="Shared knowledge base access rules",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional role-specific configuration",
    )

    @property
    def tools(self) -> list[str]:
        """**DEPRECATED（N1b，§5.1）**：白名单载体字段已删除。

        仅经 ``model_extra`` 透传保留，供存量代码/测试兼容读取
        （``control_plane`` 展示、``Simulation._initialize`` 初始授予集
        来源）。**不是权限依据**——有效权限 = 两层 Grant（§3.5）。
        """
        extra = self.model_extra or {}
        raw = extra.get("tools", [])
        return list(raw) if isinstance(raw, list) else []

    @model_validator(mode="after")
    def _validate_pool(self) -> AgentConfig:
        if self.pool is not None and self.kind != "service":
            raise ValueError(
                f"Agent '{self.agent_id}': pool config requires "
                f"kind='service' (got '{self.kind}')",
            )
        return self
