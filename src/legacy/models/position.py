"""岗位模型：Position（ACL 主体）+ 占据即继承 + 经手物归属 + 边语义声明。

组织架构由岗位承载（**岗人分离**，SPEC §4.1/§5.8）：agent 被 hire 进
岗位即**自动继承**其边与授予（Grant(position, entity)，§3.5）；经手物
（task/report/mail 账号）概念上属 position（换人不换岗、活留岗上），
不随 agent 身份迁移。**position 即 ACL 主体**（role 并入，不再单独
设计，§1.8）。

本模块是**组织架构数据面**（N2）：Position 实体、边语义声明 schema
（N3 落地为组织架构 Authority 子类）、经手物归属元数据、直派形态
接口预留、以及基于 N1a Authority 两层 Grant 的占据/继承解析 helper。
授予数据（Grant(position, entity_id) 含 priority）在配置设备
（§5.10，N1a 已实现），本模块不重复实现授权。

Design references:
- SPEC §1.8（ACL 主体 = position）/ §3.5（position 本体）/
  §4.1（占据即继承）/ §4.3（priority 分级）/ §5.8（组织架构数据面：
  Position schema + 默认边语义 + 四条治理不变量）/ §5.10（授予数据）
- KANBAN/IN_PROGRESS/2026-08-24-position-model.md（N2）
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import TYPE_CHECKING, Literal

from pydantic import (
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from my_team.devices.authority import Authority, CapabilityGrant


def _ensure_uuid4(value: uuid.UUID, field: str) -> uuid.UUID:
    """uuid4 校验（SPEC：position/agent/关联 id 一律 uuid4）。"""
    if value.version != 4:
        raise ValueError(f"{field} must be a uuid4, got {value}")
    return value


# ----------------------------------------------------------------------
# Position 实体（ACL 主体；组织架构数据，非核心结构，§3.5/§5.8）
# ----------------------------------------------------------------------


class Position(BaseModel):
    """岗位（ACL 主体，组织架构数据的核心实体）。

    - ``name``：可读名/业务标签，**非权限依据**；
    - ``jd``：职责/提示词 = org 干预 agent 的唯一杠杆（``[POSITION_JD]``
      注入工作记忆，§4.3，N4 联测）；
    - ``superior_id`` 唯一（直属上司岗位）；``subordinate_ids`` /
      ``collaborator_ids`` 为出边集合；边语义由组织架构声明
      （见 ``EdgeSemanticsDeclaration`` / ``DEFAULT_EDGE_SEMANTICS``）；
    - 授予是独立记录 ``Grant(position, entity_id)``（配置设备 §5.10），
      不在本实体上。
    """

    position_id: uuid.UUID
    name: str
    jd: str = ""
    superior_id: uuid.UUID | None = None
    subordinate_ids: list[uuid.UUID] = Field(default_factory=list)
    collaborator_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("position_id")
    @classmethod
    def _position_id_uuid4(cls, v: uuid.UUID) -> uuid.UUID:
        return _ensure_uuid4(v, "position_id")

    @model_validator(mode="after")
    def _no_self_edge(self) -> Position:
        """岗位不得把自己挂在自己的边上（superior 唯一，杜绝自环）。"""
        if self.superior_id == self.position_id:
            raise ValueError(
                f"position {self.position_id}: superior_id 不得指向自身"
            )
        if self.position_id in self.subordinate_ids:
            raise ValueError(
                f"position {self.position_id}: subordinate_ids 不得包含自身"
            )
        if self.position_id in self.collaborator_ids:
            raise ValueError(
                f"position {self.position_id}: collaborator_ids 不得包含自身"
            )
        return self


class PositionGraph:
    """岗位图（组织架构**边数据面**）只读导航。

    边语义的解释（command/request、declined、升级沿 superior 而回报
    只回请求方）在组织架构设备（N3 落地为 Authority 子类）；本类只
    提供边数据的结构化访问，不含行为。
    """

    def __init__(self, positions: Iterable[Position]) -> None:
        self._by_id: dict[uuid.UUID, Position] = {
            p.position_id: p for p in positions
        }

    def get(self, position_id: uuid.UUID) -> Position | None:
        return self._by_id.get(position_id)

    def superior(self, position_id: uuid.UUID) -> Position | None:
        """直属上司岗位（无 superior_id 或目标缺失返回 None）。"""
        p = self._by_id.get(position_id)
        if p is None or p.superior_id is None:
            return None
        return self._by_id.get(p.superior_id)

    def subordinates(self, position_id: uuid.UUID) -> list[Position]:
        """下属岗位（保持声明顺序；缺失的边目标忽略）。"""
        p = self._by_id.get(position_id)
        if p is None:
            return []
        return [self._by_id[s] for s in p.subordinate_ids if s in self._by_id]

    def collaborators(self, position_id: uuid.UUID) -> list[Position]:
        """沟通合作者岗位（保持声明顺序；缺失的边目标忽略）。"""
        p = self._by_id.get(position_id)
        if p is None:
            return []
        return [
            self._by_id[c] for c in p.collaborator_ids if c in self._by_id
        ]


# ----------------------------------------------------------------------
# 边语义声明 schema（组织架构数据面；N3 落地为 Authority 子类）
# ----------------------------------------------------------------------


class EdgeKind(str, Enum):
    """岗位边种类（§5.8）：superior / subordinate / collaborator。"""

    SUPERIOR = "superior"
    SUBORDINATE = "subordinate"
    COLLABORATOR = "collaborator"


class EdgeSemanticsDeclaration(BaseModel):
    """单条边的语义声明（组织架构数据面，N3 落地）。

    边语义 = 组织架构声明的**数据**（org 定义自己的边行为，一客一实例
    主权自治）；内核只校验四条治理不变量（§5.8/§11，见
    ``validate_governance_invariants``）。默认语义见
    ``DEFAULT_EDGE_SEMANTICS``（§5.8 默认边语义表）。

    - ``delegation_mode``：none / command（命令委派，下属不可拒绝，
      只能 fail）/ request（请求委派，可拒绝 declined + 回执）；
    - ``refusal_allowed``：仅对 request 委派合法；
    - ``escalation_target``：是否上报/escalation 对象（superior）；
    - 四个 transfer 标志是四条治理不变量的声明面（默认 False）：
      ``transfers_accountability``（授权不授责）、``veto_transferable``
      （veto 默认不可转授）、``transfers_ownership``（escalation 不转移
      ownership）。
    """

    kind: EdgeKind
    direction: Literal["in", "out", "bidirectional"]
    delegation_mode: Literal["none", "command", "request"] = "none"
    refusal_allowed: bool = False
    escalation_target: bool = False
    transfers_accountability: bool = False
    veto_transferable: bool = False
    transfers_ownership: bool = False
    description: str = ""

    @model_validator(mode="after")
    def _refusal_requires_request(self) -> EdgeSemanticsDeclaration:
        if self.refusal_allowed and self.delegation_mode != "request":
            raise ValueError(
                f"{self.kind.value}: refusal_allowed 仅对 request 委派合法"
                "（command 下属不可拒绝，只能 fail，§5.8）"
            )
        return self


DEFAULT_EDGE_SEMANTICS: dict[EdgeKind, EdgeSemanticsDeclaration] = {
    EdgeKind.SUPERIOR: EdgeSemanticsDeclaration(
        kind=EdgeKind.SUPERIOR,
        direction="in",
        escalation_target=True,
        description="唯一入边：上报/escalation 对象；不可向它委派任务（§5.8）",
    ),
    EdgeKind.SUBORDINATE: EdgeSemanticsDeclaration(
        kind=EdgeKind.SUBORDINATE,
        direction="out",
        delegation_mode="command",
        description="出边集合：可命令委派；下属不可拒绝，只能 fail（§5.8）",
    ),
    EdgeKind.COLLABORATOR: EdgeSemanticsDeclaration(
        kind=EdgeKind.COLLABORATOR,
        direction="bidirectional",
        delegation_mode="request",
        refusal_allowed=True,
        description=(
            "双向集合：可请求委派（请求帮忙），可拒绝（declined + 回执）；"
            "通信与共享上下文（§5.8）"
        ),
    ),
}


class GovernanceInvariantError(Exception):
    """边语义声明违反四条治理不变量（静态拒绝，§5.8/§11）。"""


def validate_edge_semantics(
    declarations: Iterable[EdgeSemanticsDeclaration],
) -> None:
    """声明面静态校验：四条治理不变量中的三条字段级规则。

    - 授权不授责：``transfers_accountability`` 不得为 True（委派不转移
      责任——授权者始终对结果负责）；
    - veto 默认不可转授：``veto_transferable`` 不得为 True；
    - escalation 不转移 ownership：``transfers_ownership`` 不得为 True
      （升级不把所有权转给上级）。

    第四条「委派单调」需要岗位图（见 ``validate_governance_invariants``）。
    N8 联测落地完整求值；本校验为 schema 面第一道静态闸。
    """
    errors: list[str] = []
    for decl in declarations:
        if decl.transfers_accountability:
            errors.append(
                f"{decl.kind.value}: 违反「授权不授责」— 委派不转移责任"
                "（transfers_accountability=True）"
            )
        if decl.veto_transferable:
            errors.append(
                f"{decl.kind.value}: 违反「veto 默认不可转授」— veto 权"
                "不可转授（veto_transferable=True）"
            )
        if decl.transfers_ownership:
            errors.append(
                f"{decl.kind.value}: 违反「escalation 不转移 ownership」—"
                "所有权不可随升级/委派转授（transfers_ownership=True）"
            )
    if errors:
        raise GovernanceInvariantError("\n".join(errors))


def _delegation_graph(
    positions: Iterable[Position],
    semantics: Mapping[EdgeKind, EdgeSemanticsDeclaration],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """按声明的 delegation_mode 展开委派边（subordinate/collaborator）。"""
    graph: dict[uuid.UUID, set[uuid.UUID]] = {}
    for p in positions:
        graph.setdefault(p.position_id, set())
        for kind, ids in (
            (EdgeKind.SUBORDINATE, p.subordinate_ids),
            (EdgeKind.COLLABORATOR, p.collaborator_ids),
        ):
            decl = semantics.get(kind)
            if decl is None or decl.delegation_mode == "none":
                continue
            for target in ids:
                graph.setdefault(p.position_id, set()).add(target)
    return graph


def _find_cycle(
    graph: dict[uuid.UUID, set[uuid.UUID]],
) -> list[uuid.UUID] | None:
    """在图中找一条环（DFS 三色标记）；无环返回 None。"""
    color: dict[uuid.UUID, int] = {}  # 0=白(未访) 1=灰(在栈) 2=黑(完成)
    path: list[uuid.UUID] = []

    def visit(node: uuid.UUID) -> list[uuid.UUID] | None:
        color[node] = 1
        path.append(node)
        for nxt in graph.get(node, ()):
            state = color.get(nxt, 0)
            if state == 1:
                i = path.index(nxt)
                return path[i:] + [nxt]
            if state == 0:
                cycle = visit(nxt)
                if cycle is not None:
                    return cycle
        path.pop()
        color[node] = 2
        return None

    for start in graph:
        if color.get(start, 0) == 0:
            cycle = visit(start)
            if cycle is not None:
                return cycle
    return None


def _dangling_edge_errors(positions: list[Position]) -> list[str]:
    known = {p.position_id for p in positions}
    errors: list[str] = []
    for p in positions:
        if p.superior_id is not None and p.superior_id not in known:
            errors.append(
                f"岗位 {p.position_id} 的 superior_id 指向不存在的岗位 "
                f"{p.superior_id}"
            )
        for s in p.subordinate_ids:
            if s not in known:
                errors.append(
                    f"岗位 {p.position_id} 的 subordinate_id 指向不存在的"
                    f"岗位 {s}"
                )
        for c in p.collaborator_ids:
            if c not in known:
                errors.append(
                    f"岗位 {p.position_id} 的 collaborator_id 指向不存在的"
                    f"岗位 {c}"
                )
    return errors


def validate_governance_invariants(
    positions: Iterable[Position],
    semantics: Mapping[EdgeKind, EdgeSemanticsDeclaration] | None = None,
) -> None:
    """四条治理不变量静态校验（§5.8/§11；N8 联测落地完整求值）。

    1. 授权不授责 / 2. veto 默认不可转授 / 3. escalation 不转移
       ownership：声明字段级规则（见 ``validate_edge_semantics``）；
    4. 委派单调：委派边图（按 semantics 的 delegation_mode 展开
       subordinate/collaborator 边）不得成环——委派不可绕回起点。

    另做完整性检查：边不得指向不存在的岗位（悬空边静态拒绝）。
    违反任一条抛 ``GovernanceInvariantError``。
    """
    semantics = semantics or DEFAULT_EDGE_SEMANTICS
    validate_edge_semantics(semantics.values())
    positions_list = list(positions)
    errors = _dangling_edge_errors(positions_list)
    if errors:
        raise GovernanceInvariantError("\n".join(errors))
    cycle = _find_cycle(_delegation_graph(positions_list, semantics))
    if cycle is not None:
        rendered = ", ".join(str(c) for c in cycle)
        raise GovernanceInvariantError(
            f"违反「委派单调」— 委派边图存在环: [{rendered}]"
        )


# ----------------------------------------------------------------------
# 经手物归属元数据（§5.8：task/report/mail 账号 → position_id）
# ----------------------------------------------------------------------


class ArtifactKind(str, Enum):
    """经手物种类（§5.8）：task / report / mail 账号。"""

    TASK = "task"
    REPORT = "report"
    MAIL = "mail"


class ArtifactOwnership(BaseModel):
    """经手物归属记录：账号（task id / report id / mail 账号）→ 岗位。

    经手物概念上属 position（换人不换岗、活留岗上）；记录里**没有**
    agent 字段——不随 agent 身份迁移是结构性保证（§4.1「无可持有
    资产」）。实现为归属元数据（静态先行），运行时换人策略 N3 落地。
    """

    kind: ArtifactKind
    account: str
    position_id: uuid.UUID

    @field_validator("position_id")
    @classmethod
    def _position_id_uuid4(cls, v: uuid.UUID) -> uuid.UUID:
        return _ensure_uuid4(v, "position_id")


class ArtifactOwnershipRegistry:
    """经手物归属表：(kind, account) → position_id。

    - ``owner``：按账号查归属岗位（无则 None）；
    - ``for_position``：某岗位名下的全部经手物；
    - 同一账号重复登记到不同岗位 → ``ValueError``（归属唯一）。
    """

    def __init__(
        self, ownerships: Iterable[ArtifactOwnership] = ()
    ) -> None:
        self._entries: dict[tuple[ArtifactKind, str], ArtifactOwnership] = {}
        for ownership in ownerships:
            self.add(ownership)

    def add(self, ownership: ArtifactOwnership) -> None:
        key = (ownership.kind, ownership.account)
        existing = self._entries.get(key)
        if existing is not None and existing.position_id != ownership.position_id:
            raise ValueError(
                f"经手物 {key[0].value}/{key[1]!r} 已归属岗位 "
                f"{existing.position_id}，不可改归 {ownership.position_id}"
            )
        self._entries[key] = ownership

    def owner(self, kind: ArtifactKind, account: str) -> uuid.UUID | None:
        entry = self._entries.get((kind, account))
        return entry.position_id if entry is not None else None

    def for_position(self, position_id: uuid.UUID) -> list[ArtifactOwnership]:
        return [
            o for o in self._entries.values() if o.position_id == position_id
        ]


# ----------------------------------------------------------------------
# priority 分级（§4.3：<10 固定工作记忆 / ≥10 触发召回）
# ----------------------------------------------------------------------


PRIORITY_THRESHOLD = 10


class PriorityClass(str, Enum):
    """授予 priority 分级（§4.3）：固定工作记忆 / 触发器召回。"""

    FIXED_WORKING_MEMORY = "fixed_working_memory"
    TRIGGERED_RECALL = "triggered_recall"


def priority_class(priority: int) -> PriorityClass:
    """priority < 10 → 固定工作记忆（单独预算、不可超、预算可配置，
    JD 属此类）；≥ 10 → 触发器召回（§4.3 分级）。"""
    if priority < PRIORITY_THRESHOLD:
        return PriorityClass.FIXED_WORKING_MEMORY
    return PriorityClass.TRIGGERED_RECALL


# ----------------------------------------------------------------------
# 占据/继承解析（基于 N1a Authority 两层 Grant，不重复实现授权）
# ----------------------------------------------------------------------


def effective_capabilities(
    authority: "Authority", agent_id: str
) -> list["CapabilityGrant"]:
    """占据即继承：agent 占据的全部 position 的有效能力授予并集。

    有效权限 = ∃position：Grant(agent, position) ∧ Grant(position,
    entity)（§3.5/§5.1，deny-by-default）。本 helper 基于 N1a
    Authority 的布线求值（借道其内部查询 ``_grants_for``，保留
    priority/effect 全量信息）；**N3 若重构布线存储需同步本函数**。
    """
    grants: list[CapabilityGrant] = []
    for entity_id in authority.registered:
        grants.extend(authority._grants_for(agent_id, entity_id))
    return grants


# ----------------------------------------------------------------------
# 直派形态接口预留（agent → position 直接指派，§1.8/§4.1/§5.8）
# ----------------------------------------------------------------------


class DirectAssignment(BaseModel):
    """直派形态记录（agent → position 直接指派）。

    架构灵活性选项：框架**不依赖组织架构存在**——朴素系统可不装组织
    架构，直接给 agent 指派 position（§1.8/§5.8）。N3 落地 mount
    挂载语义（岗人分离、多版本 agent 候选挂载）基于本接口扩展。
    """

    agent_id: uuid.UUID
    position_id: uuid.UUID
    note: str = ""

    @field_validator("agent_id", "position_id")
    @classmethod
    def _ids_uuid4(cls, v: uuid.UUID, info: ValidationInfo) -> uuid.UUID:
        return _ensure_uuid4(v, info.field_name or "id")


def direct_assign(
    agent_id: uuid.UUID,
    position_id: uuid.UUID,
    authority: "Authority | None" = None,
    *,
    note: str = "",
) -> DirectAssignment:
    """直派接口预留：产生直派记录；传入 Authority 时立即落地为
    Grant(agent, position)（成员授予，§5.1 布线中心）。"""
    assignment = DirectAssignment(
        agent_id=agent_id, position_id=position_id, note=note
    )
    if authority is not None:
        authority.grant_membership(str(agent_id), str(position_id))
    return assignment
