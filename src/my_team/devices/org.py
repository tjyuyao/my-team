"""组织架构设备 = Authority 子类（SPEC §5.8，N3）。

在 N1a Authority（注册中心 + 布线中心，devices/authority.py）之上叠加
组织架构数据面（N2 Position 模型，models/position.py）：

- **岗位/边/授权读改写**：岗位实体（Position）持有；边修改做双向一致
  （``superior_id`` ↔ ``subordinate_ids``）；授权读写经组织级接口
  （``grant_org_capability`` 等）；全部操作先过 ``org_manage`` 授予
  鉴权——root 级 agent 持 ``Grant(root_position, org_manage)`` 即可
  运行时做组织调整（动态优于静态，§5.8），无权限者 POLICY_DENIED；
- **JD 与上下级关系作为 memory entry**：每岗位注册 DATA 实体并授予
  该岗位（priority<10 固定工作记忆，§4.3），占据者经 N1a
  ``injection_for`` 继承（能力 = 权限 + 记忆，§5.1）；JD 即 org 干预
  agent 的唯一杠杆（``[POSITION_JD]``）；
- **边语义 = 它注册的工具能力 + 生效条件**：边语义能力实体
  （``_semantics_entities``）+ ``edge_semantics`` 声明（默认表见 N2
  ``DEFAULT_EDGE_SEMANTICS``，org 可改，不违反四条治理不变量）；
- **改边/改授权触发四条治理不变量静态校验**（N2 校验器复用，
  §5.8/§11）——违反则拒绝且不落状态、不入 Journal；
- **组织调整全程入 Journal**（审计、可回滚）：``journal_sink`` 接口
  预留——现有 TickJournal 在 simulation 层（tick 粒度），设备侧不
  直接持有；世界记忆设备接口层已裁撤（2026-08-25），sink 维持预留
  （审计经 AuditLog 落 Journal）；
- **可替换**：``register=False`` 候选 + ``replace``（直派 Authority →
  组织架构，同一接口，§5.8/§4.1；朴素系统不装本设备也能直派）。

Design references:
- SPEC §3.7（UI 插件：本设备声明 ``ui_modules`` 挂到 Control Plane）/
  §4.1（岗人分离、占据即继承）/ §4.3（priority 分级）/ §5.1（Authority
  子类、基类行为）/ §5.8（组织架构）/ §10（/org/* 操作台 API）
- KANBAN/IN_PROGRESS/2026-08-25-org-structure-device.md（N3）
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from my_team.control_plane import UIModule
from my_team.devices.authority import Authority, CapabilityGrant, GrantEffect
from my_team.devices.base import EntityKind, InjectionDecl, RegisteredEntity
from my_team.models.agent import Agent
from my_team.models.position import (
    DEFAULT_EDGE_SEMANTICS,
    DirectAssignment,
    EdgeKind,
    EdgeSemanticsDeclaration,
    Position,
    PositionGraph,
    direct_assign,
    validate_edge_semantics,
    validate_governance_invariants,
)

#: JD/关系注入授予的 priority（<10 固定工作记忆，§4.3）。
ORG_MEMORY_PRIORITY = 1


@dataclass(frozen=True)
class OrgChange:
    """一次组织调整的审计记录（SPEC §5.8：组织调整全程入 Journal）。

    设备侧统一审计载荷；``OrgStructure.journal_sink`` 为预留接口——
    现有 TickJournal 在 simulation 层（tick 粒度），设备侧不直接持有；
    世界记忆设备接口层已裁撤（2026-08-25），sink 维持预留（测试中
    接线，生产未接；组织调整审计经 AuditLog 落 Journal，§3.2/§5.8）。
    """

    op: str
    actor_agent_id: str
    position_id: str = ""
    entity_id: str = ""
    detail: str = ""


class OrgError(Exception):
    """组织架构操作失败基类。"""


class OrgPermissionDenied(OrgError):
    """无 org_manage 授予的组织调整（POLICY_DENIED，§5.8）。"""


class OrgPositionNotFound(OrgError):
    """岗位不存在。"""


class OrgDuplicatePosition(OrgError):
    """岗位重复添加。"""


class OrgStructure(Authority):
    """组织架构 = Authority 子类（§5.8）：岗位/边/授权读改写 + memory
    注入 + mount + 可替换。"""

    def __init__(
        self,
        team_id: str,
        owner_agent_id: str,
        device_id: str | None = None,
        *,
        register: bool = True,
        edge_semantics: Mapping[EdgeKind, EdgeSemanticsDeclaration] | None = None,
        journal_sink: Callable[[OrgChange], None] | None = None,
    ) -> None:
        """构造组织架构设备。

        ``register=False`` 时创建**替换候选**（不进 Team 注册表）：由
        ``replace`` 在 Owner 确认后原子接管（§5.1 安装/替换）。
        """
        super().__init__(team_id, owner_agent_id, device_id, register=register)
        self.edge_semantics: dict[EdgeKind, EdgeSemanticsDeclaration] = dict(
            edge_semantics or DEFAULT_EDGE_SEMANTICS
        )
        # 声明面静态校验：授权不授责 / veto 不可转授 / escalation 不转移
        # ownership（§5.8/§11）——组织架构不得声明违反治理不变量的边语义。
        validate_edge_semantics(self.edge_semantics.values())
        self.journal_sink = journal_sink
        self._positions: dict[uuid.UUID, Position] = {}
        # position_id -> (jd 实体, 关系实体)：岗位记忆注入的注册表。
        self._position_memory: dict[uuid.UUID, tuple[str, str]] = {}
        # 受控实体：org 管理工具包（授权主体）+ 边语义能力实体（§5.8
        # "边语义 = 它注册的工具能力 + 生效条件"）。
        self.org_manage_id = self.register_entity(
            EntityKind.TOOLPACK,
            "org-manage",
            injection=InjectionDecl(
                content=(
                    "组织架构管理：读改写岗位/边/授权、mount（岗人分离）。"
                    "root 级 agent 持本权限即可做组织调整（§5.8）。"
                ),
                source_tag="[ORG_GUIDE]",
            ),
        )
        self._semantics_entities: dict[EdgeKind, str] = {}
        for kind, decl in self.edge_semantics.items():
            self._semantics_entities[kind] = self.register_entity(
                EntityKind.TOOL,
                f"edge:{kind.value}",
                injection=InjectionDecl(
                    content=f"{decl.description}（{kind.value} 边语义，N8 求值）",
                    source_tag="[EDGE_SEMANTICS]",
                ),
            )
        # Authority 本身是 Device：把自己注册进自己的注册中心（§5.1）。
        self.accept_device(self)

    # ------------------------------------------------------------------
    # 鉴权：组织调整须持 org_manage 授予（§5.8）
    # ------------------------------------------------------------------

    def _check_org_permission(self, by_agent_id: str) -> None:
        """无授予即 POLICY_DENIED（deny-by-default，§5.1）。"""
        if not self.authorize(by_agent_id, self.org_manage_id).allowed:
            raise OrgPermissionDenied(
                f"POLICY_DENIED: agent {by_agent_id!r} 无组织架构管理权限"
                f"（缺 Grant(position, {self.org_manage_id})）"
            )

    # ------------------------------------------------------------------
    # 引导：org 初始化安装 + 初始授予集（§5.1）
    # ------------------------------------------------------------------

    def bootstrap(
        self,
        positions: Iterable[Position],
        *,
        root_agent_id: str | None = None,
        root_position_id: uuid.UUID | None = None,
    ) -> None:
        """安装初始岗位集（§5.1 引导 = org 初始化安装 + 初始授予集）。

        安装阶段不经 org_manage 鉴权（Owner 安装）；四条治理不变量
        照常校验。``root_agent_id`` + ``root_position_id`` 成对给出时
        建立 root 授予：``Grant(root_position, org_manage)`` + 成员挂载
        ——root 级 agent 由此获得运行时组织调整权（§5.8）。
        """
        new_positions = list(positions)
        for p in new_positions:
            if p.position_id in self._positions:
                raise OrgDuplicatePosition(
                    f"岗位 {p.position_id} 已存在（重复安装）"
                )
        validate_governance_invariants(
            list(self._positions.values()) + new_positions, self.edge_semantics
        )
        for p in new_positions:
            self._positions[p.position_id] = p
            self._sync_position_memory(p)
        if root_agent_id is not None:
            if root_position_id is None:
                raise OrgError("bootstrap 建立 root 授予需指定 root_position_id")
            self._require_position(root_position_id)
            self.grant_capability(
                str(root_position_id),
                self.org_manage_id,
                priority=ORG_MEMORY_PRIORITY,
            )
            self.grant_membership(root_agent_id, str(root_position_id))
            self._record(
                OrgChange(
                    op="bootstrap",
                    actor_agent_id=root_agent_id,
                    position_id=str(root_position_id),
                    detail=f"安装 {len(new_positions)} 个岗位并建立 root 授予",
                )
            )
        elif new_positions:
            self._record(
                OrgChange(
                    op="bootstrap",
                    actor_agent_id=self.owner_agent_id,
                    detail=f"安装 {len(new_positions)} 个岗位",
                )
            )

    # ------------------------------------------------------------------
    # 岗位/边/授权读改写（§5.8 能力；§10 GET /org/*）
    # ------------------------------------------------------------------

    def positions(self, by_agent_id: str) -> dict[uuid.UUID, Position]:
        """岗位清单（只读副本；§10 GET /org/positions）。"""
        self._check_org_permission(by_agent_id)
        return dict(self._positions)

    def get_position(
        self, position_id: uuid.UUID, by_agent_id: str
    ) -> Position | None:
        self._check_org_permission(by_agent_id)
        return self._positions.get(position_id)

    def graph(self, by_agent_id: str) -> PositionGraph:
        """协作网络（§10 GET /org/graph）。"""
        self._check_org_permission(by_agent_id)
        return PositionGraph(self._positions.values())

    def add_position(self, position: Position, by_agent_id: str) -> None:
        """新增岗位（读改写岗位；违反治理不变量 → 拒绝且不落状态）。"""
        self._check_org_permission(by_agent_id)
        if position.position_id in self._positions:
            raise OrgDuplicatePosition(f"岗位 {position.position_id} 已存在")
        validate_governance_invariants(
            list(self._positions.values()) + [position], self.edge_semantics
        )
        self._positions[position.position_id] = position
        self._sync_position_memory(position)
        self._record(
            OrgChange(
                op="add_position",
                actor_agent_id=by_agent_id,
                position_id=str(position.position_id),
                detail=position.name,
            )
        )

    def remove_position(self, position_id: uuid.UUID, by_agent_id: str) -> None:
        """移除岗位：清理引用边、撤销其授予/记忆实体与成员关系。"""
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        prospective = {
            pid: p.model_copy(deep=True)
            for pid, p in self._positions.items()
            if pid != position_id
        }
        for p in prospective.values():
            if p.superior_id == position_id:
                p.superior_id = None
            p.subordinate_ids = [s for s in p.subordinate_ids if s != position_id]
            p.collaborator_ids = [c for c in p.collaborator_ids if c != position_id]
        validate_governance_invariants(prospective.values(), self.edge_semantics)
        self._positions = prospective
        # 撤销该岗位的记忆注入实体与全部授予（含成员关系）。
        jd_eid, edge_eid = self._position_memory.pop(position_id, ("", ""))
        for eid in (jd_eid, edge_eid):
            if eid:
                self.revoke_capability(str(position_id), eid)
                self._entities.pop(eid, None)
                self._registry.pop(eid, None)
        self._capabilities.pop(str(position_id), None)
        for agent_id in list(self._memberships):
            self._memberships[agent_id].discard(str(position_id))
        for p in self._positions.values():
            self._refresh_relations(p)
        self._record(
            OrgChange(
                op="remove_position",
                actor_agent_id=by_agent_id,
                position_id=str(position_id),
            )
        )

    def set_jd(self, position_id: uuid.UUID, jd: str, by_agent_id: str) -> None:
        """改写 JD（org 干预 agent 的唯一杠杆，§5.8）；注入内容同步。"""
        self._check_org_permission(by_agent_id)
        position = self._require_position(position_id)
        position.jd = jd
        jd_eid, _ = self._position_memory[position_id]
        self._update_memory_content(position_id, jd_eid, jd)
        self._record(
            OrgChange(
                op="set_jd", actor_agent_id=by_agent_id,
                position_id=str(position_id),
            )
        )

    def set_superior(
        self,
        position_id: uuid.UUID,
        superior_id: uuid.UUID | None,
        by_agent_id: str,
    ) -> None:
        """设直属上级（唯一入边，双向一致；superior_id=None 解除）。"""
        self._mutate_positions(
            by_agent_id,
            "set_superior",
            position_id,
            lambda ps: self._link_superior(ps, position_id, superior_id),
        )

    def add_subordinate(
        self, manager_id: uuid.UUID, subordinate_id: uuid.UUID, by_agent_id: str
    ) -> None:
        """把 subordinate 挂到 manager 名下（= set_superior 的另一侧）。"""
        self._mutate_positions(
            by_agent_id,
            "add_subordinate",
            manager_id,
            lambda ps: self._link_superior(ps, subordinate_id, manager_id),
        )

    def remove_subordinate(
        self, manager_id: uuid.UUID, subordinate_id: uuid.UUID, by_agent_id: str
    ) -> None:
        def mutate(ps: dict[uuid.UUID, Position]) -> None:
            if manager_id not in ps or subordinate_id not in ps:
                raise OrgPositionNotFound("岗位不存在")
            ps[manager_id].subordinate_ids = [
                s for s in ps[manager_id].subordinate_ids if s != subordinate_id
            ]
            if ps[subordinate_id].superior_id == manager_id:
                ps[subordinate_id].superior_id = None

        self._mutate_positions(by_agent_id, "remove_subordinate", manager_id, mutate)

    def add_collaborator(
        self, position_id: uuid.UUID, other_id: uuid.UUID, by_agent_id: str
    ) -> None:
        """加协作出边（出边集合，N2 schema；双向语义经声明实现，§5.8）。"""

        def mutate(ps: dict[uuid.UUID, Position]) -> None:
            if position_id not in ps or other_id not in ps:
                raise OrgPositionNotFound("岗位不存在")
            if other_id not in ps[position_id].collaborator_ids:
                ps[position_id].collaborator_ids.append(other_id)

        self._mutate_positions(by_agent_id, "add_collaborator", position_id, mutate)

    def remove_collaborator(
        self, position_id: uuid.UUID, other_id: uuid.UUID, by_agent_id: str
    ) -> None:
        def mutate(ps: dict[uuid.UUID, Position]) -> None:
            if position_id not in ps:
                raise OrgPositionNotFound("岗位不存在")
            ps[position_id].collaborator_ids = [
                c for c in ps[position_id].collaborator_ids if c != other_id
            ]

        self._mutate_positions(by_agent_id, "remove_collaborator", position_id, mutate)

    def grant_capability(
        self,
        position_id: str,
        entity_id: str,
        effect: GrantEffect = GrantEffect.ALLOWED,
        priority: int = 5,
    ) -> None:
        """能力授予（组织架构版）：授予前幂等补注自身受控实体。

        被 ``replace`` 接管后（``register=False`` 候选的注册表被移交数据
        覆盖，且 replace 由旧 Authority 发起、不经本类重写路径），本设备
        的受控实体（org_manage/边语义）可能不在注册中心——授予前补注
        保证组织架构自身实体始终可被授予（§5.1 注册中心 + §5.8 可替换）。
        """
        self._ensure_own_entities_registered()
        super().grant_capability(position_id, entity_id, effect, priority)

    def grant_org_capability(
        self,
        position_id: uuid.UUID,
        entity_id: str,
        by_agent_id: str,
        *,
        effect: GrantEffect = GrantEffect.ALLOWED,
        priority: int = 5,
    ) -> None:
        """能力授予读写（Grant(position, entity)，配置设备 §5.10 数据面）。

        entity_id 须已注册（N1a 注册中心校验，未注册 → UnknownEntityError）；
        授权数据本身不改变岗位图，治理不变量以注册校验 + 边语义声明
        静态校验为闸（N8 联测落地完整求值）。
        """
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        self.grant_capability(str(position_id), entity_id, effect, priority)
        self._record(
            OrgChange(
                op="grant_capability",
                actor_agent_id=by_agent_id,
                position_id=str(position_id),
                entity_id=entity_id,
                detail=f"effect={effect.value}, priority={priority}",
            )
        )

    def revoke_org_capability(
        self, position_id: uuid.UUID, entity_id: str, by_agent_id: str
    ) -> None:
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        self.revoke_capability(str(position_id), entity_id)
        self._record(
            OrgChange(
                op="revoke_capability",
                actor_agent_id=by_agent_id,
                position_id=str(position_id),
                entity_id=entity_id,
            )
        )

    def grant_org_membership(
        self, agent_id: str, position_id: uuid.UUID, by_agent_id: str
    ) -> None:
        """成员授予读写（Grant(agent, position)，§5.1 布线中心）。"""
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        self.grant_membership(agent_id, str(position_id))
        self._record(
            OrgChange(
                op="grant_membership",
                actor_agent_id=by_agent_id,
                position_id=str(position_id),
                entity_id=agent_id,
            )
        )

    def revoke_org_membership(
        self, agent_id: str, position_id: uuid.UUID, by_agent_id: str
    ) -> None:
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        self.revoke_membership(agent_id, str(position_id))
        self._record(
            OrgChange(
                op="revoke_membership",
                actor_agent_id=by_agent_id,
                position_id=str(position_id),
                entity_id=agent_id,
            )
        )

    # ------------------------------------------------------------------
    # mount（岗人分离，§4.1/§5.8；动态评估接口预留）
    # ------------------------------------------------------------------

    def mount(
        self,
        agent: Agent,
        position_id: uuid.UUID,
        by_agent_id: str,
        *,
        note: str = "",
    ) -> DirectAssignment:
        """岗人分离挂载（静态版本）。

        基于 N2 ``DirectAssignment``/``position_ref`` + Authority
        ``grant_membership``：``Grant(agent, position)`` 成员授予 + 落
        ``agent.position_ref``（占据即继承边与授予，§4.1）。多版本候选
        （``metadata["variant"]``）可挂载同一岗位评估——N3 只做静态
        挂载，择优留 N4 动态评估接口。
        """
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        assignment = direct_assign(
            agent.agent_id, position_id, authority=self, note=note
        )
        agent.position_ref = position_id  # 占据岗位（模型层落位）
        self._record(
            OrgChange(
                op="mount",
                actor_agent_id=by_agent_id,
                position_id=str(position_id),
                entity_id=str(agent.agent_id),
                detail=f"note={note}",
            )
        )
        return assignment

    def unmount(
        self, agent_id: str, position_id: uuid.UUID, by_agent_id: str
    ) -> None:
        """解除挂载（运行时换人，§5.8）。"""
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        self.revoke_membership(agent_id, str(position_id))
        self._record(
            OrgChange(
                op="unmount",
                actor_agent_id=by_agent_id,
                position_id=str(position_id),
                entity_id=agent_id,
            )
        )

    def project_mount(
        self, position_id: uuid.UUID, by_agent_id: str
    ) -> list[CapabilityGrant]:
        """挂载投影（动态评估接口预留，N4）：挂载到岗位将继承的能力授予。

        静态版本返回该岗位的全部能力授予（占据即继承候选集，§3.5）；
        多版本候选择优（variant 对比/评估）留待 N4 在此投影上实现。
        """
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        return list(self._capabilities.get(str(position_id), []))

    # ------------------------------------------------------------------
    # 改边公共路径：拷贝 → 校验（四条治理不变量）→ 应用
    # ------------------------------------------------------------------

    def _mutate_positions(
        self,
        by_agent_id: str,
        op: str,
        position_id: uuid.UUID,
        mutate: Callable[[dict[uuid.UUID, Position]], None],
    ) -> None:
        """改边公共路径。

        违反不变量（``GovernanceInvariantError``，N2 校验器复用）或目标
        岗位缺失时拒绝：不落状态、不入 Journal。
        """
        self._check_org_permission(by_agent_id)
        self._require_position(position_id)
        prospective = {
            pid: p.model_copy(deep=True) for pid, p in self._positions.items()
        }
        mutate(prospective)
        validate_governance_invariants(prospective.values(), self.edge_semantics)
        self._positions = prospective
        for p in self._positions.values():
            self._refresh_relations(p)
        self._record(
            OrgChange(
                op=op, actor_agent_id=by_agent_id, position_id=str(position_id)
            )
        )

    @staticmethod
    def _link_superior(
        ps: dict[uuid.UUID, Position], sub: uuid.UUID, sup: uuid.UUID | None
    ) -> None:
        """把 sub 的上级设为 sup（双向一致：sup.subordinate_ids ∋ sub）。

        sup=None 仅解除上级关系；调用方已保证在岗位副本上操作。
        """
        if sup is not None and sup == sub:
            raise OrgError("岗位不得以自身为上级")
        if sup is not None and sup not in ps:
            raise OrgPositionNotFound(f"岗位 {sup} 不存在")
        if sub not in ps:
            raise OrgPositionNotFound(f"岗位 {sub} 不存在")
        pos = ps[sub]
        old = pos.superior_id
        if old is not None and old in ps and old != sup:
            ps[old].subordinate_ids = [
                s for s in ps[old].subordinate_ids if s != sub
            ]
        pos.superior_id = sup
        if sup is not None and sub not in ps[sup].subordinate_ids:
            ps[sup].subordinate_ids.append(sub)

    def _require_position(self, position_id: uuid.UUID) -> Position:
        pos = self._positions.get(position_id)
        if pos is None:
            raise OrgPositionNotFound(f"岗位 {position_id} 不存在")
        return pos

    # ------------------------------------------------------------------
    # JD 与关系 → memory entry（能力 = 权限 + 记忆，N1a 机制复用）
    # ------------------------------------------------------------------

    def _sync_position_memory(self, position: Position) -> None:
        """注册岗位的 JD/关系注入实体并授予该岗位（priority<10 固定）。

        占据者经 ``injection_for`` 继承：条目必然对应一条
        (position, entity_id) 授予（§5.1 基类行为）。
        """
        pid = position.position_id
        jd_eid = self._register_memory_entity(
            f"jd:{position.name}", position.jd, "[POSITION_JD]"
        )
        edge_eid = self._register_memory_entity(
            f"edges:{position.name}",
            self._relations_content(position),
            "[ORG_EDGE]",
        )
        self.grant_capability(str(pid), jd_eid, priority=ORG_MEMORY_PRIORITY)
        self.grant_capability(str(pid), edge_eid, priority=ORG_MEMORY_PRIORITY)
        self._position_memory[pid] = (jd_eid, edge_eid)

    def _register_memory_entity(self, label: str, content: str, tag: str) -> str:
        """注册注入实体并直接并入自己的注册中心（本设备即 Authority）。"""
        entity_id = self.register_entity(
            EntityKind.DATA, label,
            injection=InjectionDecl(content=content, source_tag=tag),
        )
        self._registry[entity_id] = self._entities[entity_id]
        return entity_id

    def _relations_content(self, position: Position) -> str:
        """渲染岗位关系（上下级/协作）为注入文本（§5.8 关系作 memory）。"""
        parts: list[str] = []
        if position.superior_id is not None:
            sup = self._positions.get(position.superior_id)
            if sup is not None:
                parts.append(f"上级岗位：{sup.name}")
        subs = [
            self._positions[s]
            for s in position.subordinate_ids
            if s in self._positions
        ]
        if subs:
            parts.append("下属岗位：" + "、".join(s.name for s in subs))
        colls = [
            self._positions[c]
            for c in position.collaborator_ids
            if c in self._positions
        ]
        if colls:
            parts.append("协作岗位：" + "、".join(c.name for c in colls))
        return "；".join(parts)

    def _refresh_relations(self, position: Position) -> None:
        """边变更后刷新关系注入内容（注册即声明，内容随改随新）。"""
        entry = self._position_memory.get(position.position_id)
        if entry is None:
            return
        _, edge_eid = entry
        self._update_memory_content(
            position.position_id, edge_eid, self._relations_content(position)
        )

    def _update_memory_content(
        self, position_id: uuid.UUID, entity_id: str, content: str
    ) -> None:
        """更新注入实体内容（设备拥有自己的注册表，解释权在设备，§5.1）。"""
        entity = self._entities[entity_id]
        injection = entity.injection
        updated = RegisteredEntity(
            entity_id=entity.entity_id,
            device_id=entity.device_id,
            kind=entity.kind,
            label=entity.label,
            injection=InjectionDecl(
                content=content,
                source_tag=injection.source_tag if injection is not None else "",
            ),
        )
        self._entities[entity_id] = updated
        self._registry[entity_id] = updated

    # ------------------------------------------------------------------
    # Journal 审计（接口预留：世界记忆设备接口层已裁撤，sink 维持预留；
    # 审计经 AuditLog 落 Journal，§3.2/§5.8）
    # ------------------------------------------------------------------

    def _record(self, change: OrgChange) -> None:
        if self.journal_sink is not None:
            self.journal_sink(change)

    # ------------------------------------------------------------------
    # 可替换：register=False 候选 + replace（§5.1/§5.8）
    # ------------------------------------------------------------------

    def replace(self, new_authority: "Authority", by_agent_id: str) -> None:
        """安装/替换权归 Owner（§5.1）。组织架构替换的额外移交：

        - 补注自身受控实体——替换候选构造时 ``register=False``，其注册
          表被移交数据覆盖，需把自身实体（org_manage/边语义）重新并入
          注册中心；
        - org → org：移交岗位数据面（岗位/边语义/journal sink），岗位
          记忆实体重新注册，撤销旧实例的岗位记忆授予避免双份注入。
        """
        old_org = self if isinstance(self, OrgStructure) else None
        new_org = new_authority if isinstance(new_authority, OrgStructure) else None
        super().replace(new_authority, by_agent_id)
        if new_org is not None:
            new_org._ensure_own_entities_registered()
            if old_org is not None:
                new_org._adopt_from(old_org)

    def _ensure_own_entities_registered(self) -> None:
        """把自身声明的受控实体并入注册中心（replace 移交后补注）。"""
        for entity in self._entities.values():
            if entity.entity_id not in self._registry:
                self._registry[entity.entity_id] = entity

    def _adopt_from(self, old: "OrgStructure") -> None:
        """org → org 替换：移交岗位数据面（见 ``replace`` docstring）。"""
        for pid, (jd_eid, edge_eid) in old._position_memory.items():
            self.revoke_capability(str(pid), jd_eid)
            self.revoke_capability(str(pid), edge_eid)
        self.edge_semantics = dict(old.edge_semantics)
        self.journal_sink = old.journal_sink
        self._positions = {
            pid: p.model_copy(deep=True) for pid, p in old._positions.items()
        }
        self._position_memory = {}
        for pos in self._positions.values():
            self._sync_position_memory(pos)
        # 旧实例的 org_manage 授予改指新实例的 org_manage 实体（root 授权
        # 延续：Grant(root_position, 组织架构实体)，§5.8）。
        for position_key, grants in self._capabilities.items():
            self._capabilities[position_key] = [
                CapabilityGrant(
                    g.position_id, self.org_manage_id, g.effect, g.priority
                )
                if g.entity_id == old.org_manage_id
                else g
                for g in grants
            ]

    # ------------------------------------------------------------------
    # 设备 UI 插件声明（§3.7：设备可注册前后端模块插件到 Control Plane）
    # ------------------------------------------------------------------

    @property
    def ui_modules(self) -> list[UIModule]:
        """本设备声明的 UI 模块（岗位管理页 + 挂载页，§10 /org/*）。"""
        return [
            UIModule(
                module_name="org.positions",
                frontend_module="org/positions-panel",
                backend_handler=self._render_positions_panel,
                description="岗位清单：JD/边/授予（组织架构设备）",
            ),
            UIModule(
                module_name="org.mount",
                frontend_module="org/mount-panel",
                backend_handler=self._render_mount_panel,
                description="岗位挂载/换人（岗人分离）",
            ),
        ]

    def _render_positions_panel(self) -> dict[str, Any]:
        """岗位管理页后端渲染（§10 GET /org/positions 的插件视图）。"""
        return {
            "positions": [
                {
                    "position_id": str(p.position_id),
                    "name": p.name,
                    "jd": p.jd,
                    "superior_id": (
                        str(p.superior_id) if p.superior_id is not None else None
                    ),
                    "subordinate_ids": [str(s) for s in p.subordinate_ids],
                    "collaborator_ids": [str(c) for c in p.collaborator_ids],
                }
                for p in self._positions.values()
            ],
            "edge_semantics": {
                kind.value: decl.description
                for kind, decl in self.edge_semantics.items()
            },
        }

    def _render_mount_panel(self) -> dict[str, Any]:
        """挂载页后端渲染（§10 POST /org/agents/{id}/mount 的插件视图）。"""
        return {
            "mounted": [
                {"agent_id": aid, "positions": sorted(ps)}
                for aid, ps in self._memberships.items()
            ],
        }
