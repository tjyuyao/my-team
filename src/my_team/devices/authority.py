"""Authority — 特殊 Device：注册中心 + 布线中心（SPEC §5.1，N1a）。

Authority 是每 Team **仅一个**、Owner 安装的特殊设备：

- **注册中心**：接收所有设备的受控 uuid 注册（``accept_device``）；
  未注册的 entity_id 无法被授予（``grant_capability`` 拒绝）；
- **布线中心**：把 Team 内 Agent 与 Device 经 position 布线——两层
  Grant：``Grant(agent, position)``（成员，``grant_membership``）+
  ``Grant(position, entity_id)``（能力，``grant_capability``）；
  deny-by-default；effect = allowed / denied / requires_approval；
- **能力 = 权限 + 记忆**：授予生效 → 注入设备声明的 content
  （``injection_for``，外加载记忆条目必然对应一条 (position,
  entity_id) 授予）；grant 带 priority（<10 固定工作记忆，≥10 触发
  召回，N4 使用）；
- **单例强制 + Owner 安装/替换**：进程内 Team 注册表保证每 Team 唯一；
  ``replace`` 仅 Owner 可执行（组织架构可替换 = N3 直派 Authority 的
  前置）；
- 本身是 Device（可自注册，``register_self``）。

求值语义：``authorize`` 返回 ``Decision``；显式 DENIED 优先于
ALLOWED（安全），REQUIRES_APPROVAL 兜底；无任何授予 → denied
（deny-by-default）。锁约束（∃position：Grant ∧ Grant ∧ 锁）中的锁
原语在内核、锁实例在设备（§3.4/§5.1），由 N1b 接线时叠加。

Design references:
- SPEC §1.8 / §3.5 / §5.1
- KANBAN/TODO/2026-08-24-device-protocol-authority.md（N1a）
- 注意：本模块与旧 ``my_team/authority.py``（8 域裁决模型，E2→N5）
  同名异义——旧模型是"多方主张裁决"，本模块是"注册与布线"。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from my_team.devices.base import Device, EntityKind, RegisteredEntity

if TYPE_CHECKING:
    pass


class GrantEffect(str, Enum):
    """授予的效果（§5.1）：allowed / denied / requires_approval。"""

    ALLOWED = "allowed"
    DENIED = "denied"
    REQUIRES_APPROVAL = "requires_approval"


class AuthorityError(Exception):
    """Authority 操作失败基类。"""


class DuplicateAuthorityError(AuthorityError):
    """每 Team 仅一个 Authority（单例强制）。"""


class UnknownEntityError(AuthorityError):
    """向未注册的 entity_id 授予（注册中心校验）。"""


class NotOwnerError(AuthorityError):
    """安装/替换权仅归 Owner。"""


@dataclass(frozen=True)
class MembershipGrant:
    """Grant(agent, position)：成员授予（agent 占据 position）。"""

    agent_id: str
    position_id: str


@dataclass(frozen=True)
class CapabilityGrant:
    """Grant(position, entity_id)：能力授予（含 priority）。

    priority 语义（§4.3）：<10 固定工作记忆（单独预算、不可超、可配置，
    JD 属此类）；≥10 触发器召回。
    """

    position_id: str
    entity_id: str
    effect: GrantEffect = GrantEffect.ALLOWED
    priority: int = 5


@dataclass(frozen=True)
class Decision:
    """一次授权求值的结果。"""

    effect: GrantEffect
    entity_id: str = ""
    position_id: str | None = None

    @property
    def allowed(self) -> bool:
        """放行 = allowed 或 requires_approval（有授予即进效果路径）。"""
        return self.effect is not GrantEffect.DENIED


@dataclass(frozen=True)
class MemoryInjection:
    """能力 = 权限 + 记忆：一条外加载记忆条目（N4 联测钩子）。

    必然对应一条 (position, entity_id) 授予；content 由设备声明
    （注册即声明注入内容），priority 来自该授予。
    """

    entity_id: str
    source_device_id: str
    content: str
    source_tag: str
    position_id: str
    priority: int


# 进程内 Team 注册表：单例强制的实现载体（每 Team 唯一 Authority）。
_TEAM_AUTHORITIES: dict[str, "Authority"] = {}


def authority_for(team_id: str) -> "Authority | None":
    """按 Team 查唯一 Authority（无则 None）。"""
    return _TEAM_AUTHORITIES.get(team_id)


class Authority(Device):
    """注册中心 + 布线中心（每 Team 唯一，Owner 安装/替换）。"""

    def __init__(
        self,
        team_id: str,
        owner_agent_id: str,
        device_id: str | None = None,
        *,
        register: bool = True,
    ) -> None:
        """构造 Authority。

        ``register=False`` 时创建**替换候选**（不进 Team 注册表，不触发
        单例校验）：由 ``replace`` 在 Owner 确认后原子接管——新实例先
        备好，替换动作才生效（组织架构可替换，N3 前置）。
        """
        super().__init__(device_id)
        self.team_id = team_id
        self.owner_agent_id = owner_agent_id
        if register:
            if team_id in _TEAM_AUTHORITIES:
                raise DuplicateAuthorityError(
                    f"team {team_id!r} already has an Authority"
                )
            _TEAM_AUTHORITIES[team_id] = self
        self._registry: dict[str, RegisteredEntity] = {}
        self._memberships: dict[str, set[str]] = {}  # agent_id -> positions
        self._capabilities: dict[str, list[CapabilityGrant]] = {}

    # ------------------------------------------------------------------
    # 注册中心
    # ------------------------------------------------------------------

    def accept_device(self, device: Device) -> None:
        """接收设备全部受控 uuid 注册（§5.1 动态注册）。"""
        for entity in device.entities.values():
            if entity.entity_id in self._registry:
                raise AuthorityError(
                    f"entity {entity.entity_id!r} already registered"
                )
            self._registry[entity.entity_id] = entity

    def register_self(self, label: str = "authority") -> str:
        """Authority 本身是 Device：向自己注册一个受控实体。"""
        entity_id = self.register_entity(EntityKind.DATA, label)
        self.accept_device(self)
        return entity_id

    @property
    def registered(self) -> dict[str, RegisteredEntity]:
        """注册中心全表（只读）。"""
        return dict(self._registry)

    def is_registered(self, entity_id: str) -> bool:
        return entity_id in self._registry

    # ------------------------------------------------------------------
    # 布线中心：两层 Grant
    # ------------------------------------------------------------------

    def grant_membership(self, agent_id: str, position_id: str) -> None:
        """Grant(agent, position)：agent 占据 position。"""
        self._memberships.setdefault(agent_id, set()).add(position_id)

    def revoke_membership(self, agent_id: str, position_id: str) -> None:
        self._memberships.get(agent_id, set()).discard(position_id)

    def grant_capability(
        self,
        position_id: str,
        entity_id: str,
        effect: GrantEffect = GrantEffect.ALLOWED,
        priority: int = 5,
    ) -> None:
        """Grant(position, entity_id)：能力授予。

        未注册的 entity_id 拒绝授予（注册中心校验）。
        """
        if entity_id not in self._registry:
            raise UnknownEntityError(
                f"entity {entity_id!r} not registered with Authority"
            )
        self._capabilities.setdefault(position_id, []).append(
            CapabilityGrant(
                position_id=position_id,
                entity_id=entity_id,
                effect=effect,
                priority=priority,
            )
        )

    def revoke_capability(self, position_id: str, entity_id: str) -> None:
        grants = self._capabilities.get(position_id, [])
        self._capabilities[position_id] = [
            g for g in grants if g.entity_id != entity_id
        ]

    # ------------------------------------------------------------------
    # 求值：deny-by-default 两层 Grant
    # ------------------------------------------------------------------

    def authorize(self, agent_id: str, entity_id: str) -> Decision:
        """有效权限 = ∃position：Grant(agent, position) ∧ Grant(position,
        entity_id)，deny-by-default（锁约束由 N1b 接线叠加）。

        合并语义：显式 DENIED 优先（安全）；ALLOWED 其次；
        REQUIRES_APPROVAL 兜底。
        """
        if entity_id not in self._registry:
            return Decision(GrantEffect.DENIED, entity_id=entity_id)
        grants = self._grants_for(agent_id, entity_id)
        if not grants:
            return Decision(GrantEffect.DENIED, entity_id=entity_id)
        for g in grants:
            if g.effect is GrantEffect.DENIED:
                return Decision(
                    GrantEffect.DENIED, entity_id=entity_id,
                    position_id=g.position_id,
                )
        for g in grants:
            if g.effect is GrantEffect.ALLOWED:
                return Decision(
                    GrantEffect.ALLOWED, entity_id=entity_id,
                    position_id=g.position_id,
                )
        g0 = grants[0]
        return Decision(
            GrantEffect.REQUIRES_APPROVAL, entity_id=entity_id,
            position_id=g0.position_id,
        )

    def _grants_for(self, agent_id: str, entity_id: str) -> list[CapabilityGrant]:
        grants: list[CapabilityGrant] = []
        for position_id in self._memberships.get(agent_id, set()):
            for g in self._capabilities.get(position_id, []):
                if g.entity_id == entity_id:
                    grants.append(g)
        return grants

    # ------------------------------------------------------------------
    # 能力 = 权限 + 记忆：注入接线（N4 联测钩子）
    # ------------------------------------------------------------------

    def injection_for(self, agent_id: str) -> list[MemoryInjection]:
        """授予生效 → 注入设备声明的 content。

        外加载记忆条目必然对应一条 (position, entity_id) 授予
        （§4.2/§5.1）：对 agent 所有 position 的能力授予，收集设备声明
        的注入内容。N4 联测：这些条目进入工作记忆（priority <10 固定 /
        ≥10 触发召回）。
        """
        injections: list[MemoryInjection] = []
        for position_id in self._memberships.get(agent_id, set()):
            for g in self._capabilities.get(position_id, []):
                entity = self._registry.get(g.entity_id)
                if entity is None or entity.injection is None:
                    continue
                injections.append(
                    MemoryInjection(
                        entity_id=entity.entity_id,
                        source_device_id=entity.device_id,
                        content=entity.injection.content,
                        source_tag=entity.injection.source_tag,
                        position_id=position_id,
                        priority=g.priority,
                    )
                )
        return injections

    # ------------------------------------------------------------------
    # 单例/Owner：安装与替换
    # ------------------------------------------------------------------

    def replace(self, new_authority: "Authority", by_agent_id: str) -> None:
        """安装/替换权归 Owner（组织架构可替换 = N3 前置）。

        由 Owner 发起；移交注册表与布线数据，Team 注册表指向新实例。
        """
        if by_agent_id != self.owner_agent_id:
            raise NotOwnerError(
                f"replace requires owner {self.owner_agent_id!r}, got "
                f"{by_agent_id!r}"
            )
        if new_authority.team_id != self.team_id:
            raise AuthorityError("replacement must belong to the same team")
        # 移交数据（注册中心 + 布线）
        new_authority._registry = dict(self._registry)
        new_authority._memberships = {
            aid: set(ps) for aid, ps in self._memberships.items()
        }
        new_authority._capabilities = {
            pid: list(gs) for pid, gs in self._capabilities.items()
        }
        _TEAM_AUTHORITIES[self.team_id] = new_authority


def new_team_id() -> str:
    """Team uuid 分配（一人公司：一实例一 Team）。"""
    return str(uuid.uuid4())
