"""N2: Position 岗位模型 + Agent uuid4 + 占据继承（SPEC §1.8/§3.5/§4.1/
§5.8，KANBAN/IN_PROGRESS/2026-08-24-position-model.md）。

覆盖卡面验收（N2 侧）：
- 验收 1：agent 占据 position 后继承边与授予（两层 Grant，有测试）；
- 验收 2：产物（task/report/mail 账号）归属 position，不随 agent 身份迁移；
- 验收 4：priority <10 固定工作记忆（persistent 预算独立可配）；
      ≥10 触发召回（分类接口，N4 联测）；
- 验收 6：直派形态（agent → position 直接指派）接口预留；
- 验收 3/5/7 涉及 N8/N1/N4 联测：出边语义声明 schema + 四条治理不变量
  静态校验接口（N8 联测落地）、新 Agent 模型（§4.1）——联测后续。
"""

from __future__ import annotations

import uuid

import pytest

from my_team.devices import (
    Authority,
    ConfigDevice,
    EntityKind,
    GrantEffect,
    InjectionDecl,
    MemoryBudget,
    new_team_id,
)
from my_team.devices.base import Device
from my_team.models import (
    DEFAULT_EDGE_SEMANTICS,
    Agent,
    AgentConfig,
    AgentKind,
    ArtifactKind,
    ArtifactOwnership,
    ArtifactOwnershipRegistry,
    DirectAssignment,
    EdgeKind,
    EdgeSemanticsDeclaration,
    GovernanceInvariantError,
    PoolConfig,
    Position,
    PositionGraph,
    PriorityClass,
    direct_assign,
    effective_capabilities,
    priority_class,
    validate_edge_semantics,
    validate_governance_invariants,
)


class SampleDevice(Device):
    """带受控实体与注入声明的最小设备（注册中心 + 注入钩子）。"""

    def __init__(self, device_id: str | None = None) -> None:
        super().__init__(device_id)
        self.page_id = self.register_entity(
            EntityKind.DATA,
            "kb-page",
            injection=InjectionDecl(
                content="页面权限说明", source_tag="[KB_GUIDE]"
            ),
        )
        self.tool_id = self.register_entity(EntityKind.TOOL, "kb_read")
        self.secret_id = self.register_entity(
            EntityKind.DATA,
            "secret-records",
            injection=InjectionDecl(
                content="机密记录访问说明", source_tag="[KB_GUIDE]"
            ),
        )


def make_team() -> tuple[str, Authority, str]:
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    return team_id, Authority(team_id, owner), owner


# ----------------------------------------------------------------------
# Position 实体（§3.5/§5.8 schema）
# ----------------------------------------------------------------------


def test_position_entity_schema():
    pid, sup, sub, coll = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p = Position(
        position_id=pid,
        name="资深研究员",
        jd="负责调研与报告",
        superior_id=sup,
        subordinate_ids=[sub],
        collaborator_ids=[coll],
    )
    assert p.position_id == pid
    assert p.name == "资深研究员"
    assert p.jd == "负责调研与报告"
    assert p.superior_id == sup
    assert p.subordinate_ids == [sub]
    assert p.collaborator_ids == [coll]


def test_position_rejects_non_uuid4():
    with pytest.raises(ValueError):
        Position(position_id=uuid.uuid1(), name="x")  # uuid1 非 uuid4
    with pytest.raises(ValueError):
        Position(position_id="pos-1", name="x")  # 非 uuid 字符串


def test_position_rejects_self_edge():
    pid = uuid.uuid4()
    with pytest.raises(ValueError, match="superior_id"):
        Position(position_id=pid, name="x", superior_id=pid)
    with pytest.raises(ValueError, match="subordinate_ids"):
        Position(position_id=pid, name="x", subordinate_ids=[pid])
    with pytest.raises(ValueError, match="collaborator_ids"):
        Position(position_id=pid, name="x", collaborator_ids=[pid])


def test_position_graph_edge_navigation():
    sup, sub1, sub2, coll = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    manager = Position(
        position_id=sup,
        name="manager",
        subordinate_ids=[sub1, sub2],
        collaborator_ids=[coll],
    )
    worker1 = Position(position_id=sub1, name="w1", superior_id=sup)
    worker2 = Position(position_id=sub2, name="w2", superior_id=sup)
    peer = Position(position_id=coll, name="peer")
    graph = PositionGraph([manager, worker1, worker2, peer])

    assert graph.superior(sub1) is manager
    assert graph.subordinates(sup) == [worker1, worker2]
    assert graph.collaborators(sup) == [peer]
    assert graph.superior(coll) is None
    assert graph.subordinates(sub1) == []
    assert graph.get(uuid.uuid4()) is None


# ----------------------------------------------------------------------
# Agent 新模型（SPEC §4.1）
# ----------------------------------------------------------------------


def test_agent_model_schema():
    aid, pos = uuid.uuid4(), uuid.uuid4()
    a = Agent(
        agent_id=aid,
        kind="llm",
        position_ref=pos,
        llm_profile="default",
        metadata={"variant": "v2"},
    )
    assert a.agent_id == aid
    assert a.kind is AgentKind.LLM
    assert a.position_ref == pos
    assert a.llm_profile == "default"
    assert a.metadata == {"variant": "v2"}  # 多版本候选预留（N3 mount）


def test_agent_kind_requires_its_field():
    with pytest.raises(ValueError, match="llm_profile"):
        Agent(agent_id=uuid.uuid4(), kind="llm")
    with pytest.raises(ValueError, match="human_queue"):
        Agent(agent_id=uuid.uuid4(), kind="human")
    with pytest.raises(ValueError, match="service_ref"):
        Agent(agent_id=uuid.uuid4(), kind="service")
    a = Agent(agent_id=uuid.uuid4(), kind="service", service_ref="pool-1")
    assert a.service_ref == "pool-1"


def test_agent_kind_fields_mutually_exclusive():
    with pytest.raises(ValueError, match="human_queue"):
        Agent(
            agent_id=uuid.uuid4(),
            kind="llm",
            llm_profile="default",
            human_queue="inbox",
        )
    a = Agent(agent_id=uuid.uuid4(), kind="human", human_queue="inbox")
    assert a.human_queue == "inbox"


def test_agent_rejects_non_uuid4():
    with pytest.raises(ValueError):
        Agent(agent_id="agent.root", kind="llm", llm_profile="default")


# ----------------------------------------------------------------------
# 验收 1：占据即继承（边与授予，两层 Grant）
# ----------------------------------------------------------------------


def test_occupancy_inherits_grants():
    _, auth, _ = make_team()
    dev = SampleDevice()
    dev.register_to(auth)
    agent_id, position_id = str(uuid.uuid4()), str(uuid.uuid4())
    auth.grant_membership(agent_id, position_id)
    auth.grant_capability(position_id, dev.tool_id, priority=5)
    auth.grant_capability(position_id, dev.page_id, priority=20)

    grants = effective_capabilities(auth, agent_id)
    by_entity = {g.entity_id: g for g in grants}
    assert by_entity[dev.tool_id].priority == 5
    assert by_entity[dev.page_id].priority == 20
    # 与求值面一致：authorize 放行（两层 Grant）
    assert auth.authorize(agent_id, dev.tool_id).allowed
    assert auth.authorize(agent_id, dev.page_id).position_id == position_id
    # deny-by-default：非成员 agent 无任何授予
    stranger = str(uuid.uuid4())
    assert effective_capabilities(auth, stranger) == []
    assert not auth.authorize(stranger, dev.tool_id).allowed


def test_occupancy_inherits_grants_union_over_positions():
    _, auth, _ = make_team()
    dev = SampleDevice()
    dev.register_to(auth)
    agent_id = str(uuid.uuid4())
    pos1, pos2 = str(uuid.uuid4()), str(uuid.uuid4())
    auth.grant_membership(agent_id, pos1)
    auth.grant_membership(agent_id, pos2)
    auth.grant_capability(pos1, dev.tool_id)
    auth.grant_capability(pos2, dev.page_id)
    entities = {g.entity_id for g in effective_capabilities(auth, agent_id)}
    assert entities == {dev.tool_id, dev.page_id}


def test_occupancy_requires_both_layers():
    _, auth, _ = make_team()
    dev = SampleDevice()
    dev.register_to(auth)
    agent_id, position_id = str(uuid.uuid4()), str(uuid.uuid4())
    # 只有能力授予、无成员 → 无有效授予（两层 Grant 缺一不可）
    auth.grant_capability(position_id, dev.tool_id)
    assert effective_capabilities(auth, agent_id) == []
    assert not auth.authorize(agent_id, dev.tool_id).allowed


def test_occupancy_inherits_edges_via_position():
    """占据即继承「边」：agent 经 position_ref 进入岗位图协作拓扑。"""
    sup, sub = uuid.uuid4(), uuid.uuid4()
    manager = Position(position_id=sup, name="manager", subordinate_ids=[sub])
    worker = Position(position_id=sub, name="worker", superior_id=sup)
    graph = PositionGraph([manager, worker])
    # 占据岗位的 agent（经 position_ref）继承岗位的边：
    # worker 岗位的 superior 边 = manager 岗位
    assert graph.superior(sub) == manager
    assert graph.subordinates(sup) == [worker]


# ----------------------------------------------------------------------
# 验收 2：经手物归属 position，不随 agent 身份迁移
# ----------------------------------------------------------------------


def test_artifact_ownership_binds_position_not_agent():
    pos_a, pos_b = uuid.uuid4(), uuid.uuid4()
    reg = ArtifactOwnershipRegistry(
        [
            ArtifactOwnership(kind=ArtifactKind.TASK, account="task-42", position_id=pos_a),
            ArtifactOwnership(kind=ArtifactKind.MAIL, account="support@corp", position_id=pos_a),
            ArtifactOwnership(kind=ArtifactKind.REPORT, account="r-2026", position_id=pos_b),
        ]
    )
    assert reg.owner(ArtifactKind.TASK, "task-42") == pos_a
    assert reg.owner(ArtifactKind.MAIL, "support@corp") == pos_a
    assert reg.owner(ArtifactKind.REPORT, "r-2026") == pos_b
    assert reg.owner(ArtifactKind.TASK, "task-99") is None
    # 换人不换岗：归属记录**没有 agent 字段**（结构性保证，§4.1「无可
    # 持有资产」）——岗位换人后归属不变，活留岗上（§5.8）
    assert "agent_id" not in ArtifactOwnership.model_fields
    accounts = {o.account for o in reg.for_position(pos_a)}
    assert accounts == {"task-42", "support@corp"}


def test_artifact_ownership_duplicate_account_rejected():
    reg = ArtifactOwnershipRegistry()
    reg.add(
        ArtifactOwnership(
            kind=ArtifactKind.MAIL, account="a@x", position_id=uuid.uuid4()
        )
    )
    with pytest.raises(ValueError, match="已归属"):
        reg.add(
            ArtifactOwnership(
                kind=ArtifactKind.MAIL, account="a@x", position_id=uuid.uuid4()
            )
        )


# ----------------------------------------------------------------------
# 验收 4：priority 分级（<10 固定工作记忆 / ≥10 触发召回）
# ----------------------------------------------------------------------


def test_priority_classification_threshold():
    assert priority_class(0) is PriorityClass.FIXED_WORKING_MEMORY
    assert priority_class(9) is PriorityClass.FIXED_WORKING_MEMORY
    assert priority_class(10) is PriorityClass.TRIGGERED_RECALL
    assert priority_class(100) is PriorityClass.TRIGGERED_RECALL


def test_priority_fixed_vs_recall_via_authority_injection():
    """grant 带 priority：<10 固定工作记忆，≥10 触发召回（§4.3）。"""
    _, auth, _ = make_team()
    dev = SampleDevice()
    dev.register_to(auth)
    agent_id, position_id = str(uuid.uuid4()), str(uuid.uuid4())
    auth.grant_membership(agent_id, position_id)
    auth.grant_capability(position_id, dev.page_id, priority=5)  # <10 固定
    auth.grant_capability(position_id, dev.secret_id, priority=20)  # ≥10 召回
    injections = auth.injection_for(agent_id)
    classes = {i.entity_id: priority_class(i.priority) for i in injections}
    assert classes[dev.page_id] is PriorityClass.FIXED_WORKING_MEMORY
    assert classes[dev.secret_id] is PriorityClass.TRIGGERED_RECALL


def test_memory_budget_independently_configurable():
    """persistent 预算独立可配（N1a ConfigDevice 数据面，§5.10）。"""
    default_budget = MemoryBudget()
    assert default_budget.fixed_memory_tokens > 0
    tight = MemoryBudget(fixed_memory_tokens=512, recall_memory_tokens=1024)
    cfg = ConfigDevice()
    cfg.memory_budget = tight
    assert cfg.memory_budget.fixed_memory_tokens == 512
    assert cfg.memory_budget.recall_memory_tokens == 1024


# ----------------------------------------------------------------------
# 验收 6：直派形态接口预留（agent → position 直接指派）
# ----------------------------------------------------------------------


def test_direct_assignment_record():
    agent_id, position_id = uuid.uuid4(), uuid.uuid4()
    assignment = direct_assign(agent_id, position_id, note="直派预留")
    assert isinstance(assignment, DirectAssignment)
    assert assignment.agent_id == agent_id
    assert assignment.position_id == position_id
    assert assignment.note == "直派预留"


def test_direct_assignment_applies_membership_via_authority():
    _, auth, _ = make_team()
    dev = SampleDevice()
    dev.register_to(auth)
    agent_id, position_id = uuid.uuid4(), uuid.uuid4()
    direct_assign(agent_id, position_id, authority=auth)  # Grant(agent, position)
    auth.grant_capability(str(position_id), dev.tool_id)
    # 直派 = 不经组织架构直接入岗：两层 Grant 成立即可用
    assert auth.authorize(str(agent_id), dev.tool_id).effect is (
        GrantEffect.ALLOWED
    )


# ----------------------------------------------------------------------
# 边语义声明 schema + 四条治理不变量（验收 3 接口，N8 联测落地）
# ----------------------------------------------------------------------


def test_default_edge_semantics_matches_spec_table():
    s = DEFAULT_EDGE_SEMANTICS
    assert s[EdgeKind.SUPERIOR].direction == "in"
    assert s[EdgeKind.SUPERIOR].escalation_target is True
    assert s[EdgeKind.SUPERIOR].delegation_mode == "none"  # 不可向它委派
    assert s[EdgeKind.SUBORDINATE].direction == "out"
    assert s[EdgeKind.SUBORDINATE].delegation_mode == "command"
    assert s[EdgeKind.SUBORDINATE].refusal_allowed is False  # 只能 fail
    assert s[EdgeKind.COLLABORATOR].direction == "bidirectional"
    assert s[EdgeKind.COLLABORATOR].delegation_mode == "request"
    assert s[EdgeKind.COLLABORATOR].refusal_allowed is True  # declined+回执


def test_refusal_requires_request_mode():
    with pytest.raises(ValueError, match="refusal_allowed"):
        EdgeSemanticsDeclaration(
            kind=EdgeKind.SUBORDINATE,
            direction="out",
            delegation_mode="command",
            refusal_allowed=True,
        )
    ok = EdgeSemanticsDeclaration(
        kind=EdgeKind.COLLABORATOR,
        direction="bidirectional",
        delegation_mode="request",
        refusal_allowed=True,
    )
    assert ok.refusal_allowed


def test_governance_invariants_reject_transfer_flags():
    with pytest.raises(GovernanceInvariantError, match="授权不授责"):
        validate_edge_semantics(
            [
                EdgeSemanticsDeclaration(
                    kind=EdgeKind.SUBORDINATE,
                    direction="out",
                    delegation_mode="command",
                    transfers_accountability=True,
                )
            ]
        )
    with pytest.raises(GovernanceInvariantError, match="veto"):
        validate_edge_semantics(
            [
                EdgeSemanticsDeclaration(
                    kind=EdgeKind.SUPERIOR,
                    direction="in",
                    veto_transferable=True,
                )
            ]
        )
    with pytest.raises(GovernanceInvariantError, match="ownership"):
        validate_edge_semantics(
            [
                EdgeSemanticsDeclaration(
                    kind=EdgeKind.SUBORDINATE,
                    direction="out",
                    delegation_mode="command",
                    transfers_ownership=True,
                )
            ]
        )


def test_governance_invariants_ok_for_defaults():
    validate_edge_semantics(DEFAULT_EDGE_SEMANTICS.values())  # 无异常


def test_governance_invariants_delegation_cycle_rejected():
    """委派单调：委派边图成环 → 静态拒绝。"""
    a, b = uuid.uuid4(), uuid.uuid4()
    pa = Position(position_id=a, name="a", subordinate_ids=[b])
    pb = Position(position_id=b, name="b", subordinate_ids=[a])
    with pytest.raises(GovernanceInvariantError, match="委派单调"):
        validate_governance_invariants([pa, pb])


def test_governance_invariants_acyclic_ok():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pa = Position(position_id=a, name="a", subordinate_ids=[b])
    pb = Position(position_id=b, name="b", superior_id=a, subordinate_ids=[c])
    pc = Position(position_id=c, name="c", superior_id=b)
    validate_governance_invariants([pa, pb, pc])  # 无异常


def test_governance_invariants_dangling_edge_rejected():
    a, missing = uuid.uuid4(), uuid.uuid4()
    pa = Position(position_id=a, name="a", subordinate_ids=[missing])
    with pytest.raises(GovernanceInvariantError, match="不存在"):
        validate_governance_invariants([pa])


# ----------------------------------------------------------------------
# 兼容性锚点：旧 AgentConfig 保留（N1b/N3 联调前不得破坏存量）
# ----------------------------------------------------------------------


def test_agent_config_legacy_still_works():
    cfg = AgentConfig(
        agent_id="agent.root",
        display_name="Root",
        role="root_decision_agent",
        parent_id=None,
        children=["agent.worker"],
        tools=["read", "write"],
        can_delegate=True,
    )
    assert cfg.agent_id == "agent.root"
    assert cfg.role == "root_decision_agent"
    assert cfg.children == ["agent.worker"]
    assert cfg.parent_id is None
    assert cfg.tools == ["read", "write"]
    assert cfg.kind == "llm"
    # 旧规则保留：pool 仅限 kind=service
    with pytest.raises(ValueError, match="pool config requires"):
        AgentConfig(agent_id="a", display_name="A", role="r", pool=PoolConfig())
