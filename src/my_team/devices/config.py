"""配置设备（SPEC §5.10，N1a）。

配置设备是**数据面**：授予数据（Grant(position, entity_id) 含
priority）、策略数据（allowlist、审批配置）、限额参数（§3.8 各维度；
含容量参数与 persistent 记忆预算 §4.3）。

- 数据来源：场景包/配置文件（T13 在其上加载）；本模块为内存数据
  容器 + 模型；
- 行为面在 Authority（布线中心消费授予数据）与内核（allowlist/
  审批求值，§3.5）；本设备只持有数据并支持"初始授予集 → Authority"
  的引导（org 初始化，§5.1）。
- 现有 ``models/activation.ExecutionConfig`` 的容量字段在 N1c 归位
  时迁入 ``CapacityLimits``（N1a 只立数据面，不碰现有代码）。

Design references:
- SPEC §3.5 / §3.8 / §4.3 / §5.10
- KANBAN/TODO/2026-08-24-device-protocol-authority.md（N1a）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from my_team.devices.authority import (
    Authority,
    CapabilityGrant,
    GrantEffect,
    MembershipGrant,
)
from my_team.devices.base import Device


@dataclass(frozen=True)
class CapacityLimits:
    """抗超负荷限额参数（§3.8 各维度；与 ExecutionConfig 归位对接）。"""

    max_active_agents_per_tick: int = 8
    max_concurrent_llm_requests: int = 4
    max_llm_calls_per_activation: int = 4
    max_tool_calls_per_activation: int = 16
    max_action_budget: int = 64
    max_delegation_depth: int = 6
    private_storage_limit_mb: int = 64
    max_output_bytes: int = 64_000


@dataclass(frozen=True)
class MemoryBudget:
    """记忆预算（§4.3）：priority<10 固定工作记忆的单独预算（不可超、
    可配置）。"""

    fixed_memory_tokens: int = 4_000
    recall_memory_tokens: int = 8_000


@dataclass
class ApprovalConfig:
    """审批配置数据（§5.10）：entity_id → 触发审批的条件描述。

    求值在内核（§3.5 三查分离中的审批态）；此处为数据容器。
    """

    requires_approval: dict[str, str] = field(default_factory=dict)


class ConfigDevice(Device):
    """配置设备：授予/策略/限额数据面（§5.10）。"""

    def __init__(self, device_id: str | None = None) -> None:
        super().__init__(device_id)
        self.memberships: list[MembershipGrant] = []
        self.capabilities: list[CapabilityGrant] = []
        self.allowlist: set[str] = set()  # 工具 allowlist 数据（entity_id）
        self.approval: ApprovalConfig = ApprovalConfig()
        self.limits: CapacityLimits = CapacityLimits()
        self.memory_budget: MemoryBudget = MemoryBudget()

    # ------------------------------------------------------------------
    # 授予数据（org 初始化 / 场景包加载后填充）
    # ------------------------------------------------------------------

    def add_membership(self, agent_id: str, position_id: str) -> None:
        self.memberships.append(MembershipGrant(agent_id, position_id))

    def add_capability(
        self,
        position_id: str,
        entity_id: str,
        effect: GrantEffect = GrantEffect.ALLOWED,
        priority: int = 5,
    ) -> None:
        self.capabilities.append(
            CapabilityGrant(position_id, entity_id, effect, priority)
        )

    def apply_to(self, authority: Authority) -> None:
        """初始授予集 → Authority（引导 = org 初始化 + 初始授予集）。

        未注册 entity_id 会抛 UnknownEntityError（注册中心校验），
        保证授予只作用于已注册受控 uuid。
        """
        for m in self.memberships:
            authority.grant_membership(m.agent_id, m.position_id)
        for c in self.capabilities:
            authority.grant_capability(
                c.position_id, c.entity_id, c.effect, c.priority
            )
