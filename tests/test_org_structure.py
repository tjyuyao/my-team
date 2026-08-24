"""N3: 组织架构设备（Authority 子类）+ Control Plane 内核化与设备 UI 插件
（SPEC §3.7/§4.1/§4.3/§5.1/§5.8/§10，KANBAN/IN_PROGRESS/
2026-08-25-org-structure-device.md）。

覆盖卡面验收：
- 验收 1：持权限的 agent 可读改写岗位/边/授权；无权限者 POLICY_DENIED；
- 验收 2：上下级关系与 JD 作为 memory entry 注入占据者（priority<10）；
- 验收 3：组织调整入 Journal 可审计（journal_sink 接口预留，N1c 接真实
      Journal）；违反治理不变量被拒且不入 Journal；
- 验收 4：设备注册 UI 插件后 Control Plane 渲染对应模块；
- 验收 5：mount 接口可用（静态版本；动态评估接口 project_mount 预留）；
- 验收 6：直派 Authority 可替换组织架构（同一接口，register=False +
      replace）。
"""

from __future__ import annotations

import uuid

import pytest

from my_team.control_plane import ControlPlane
from my_team.devices import (
    Authority,
    DuplicateAuthorityError,
    EntityKind,
    GrantEffect,
    InjectionDecl,
    NotOwnerError,
    OrgChange,
    OrgPermissionDenied,
    OrgStructure,
    authority_for,
    new_team_id,
)
from my_team.devices.base import Device
from my_team.models import (
    PRIORITY_THRESHOLD,
    Agent,
    AgentKind,
    GovernanceInvariantError,
    Position,
    PriorityClass,
    priority_class,
)


class SampleDevice(Device):
    """带受控实体与注入声明的最小设备（N1a 测试模式复用）。"""

    def __init__(self, device_id: str | None = None) -> None:
        super().__init__(device_id)
        self.tool_id = self.register_entity(EntityKind.TOOL, "kb_read")
        self.page_id = self.register_entity(
            EntityKind.DATA,
            "kb-page",
            injection=InjectionDecl(
                content="页面权限说明", source_tag="[KB_GUIDE]"
            ),
        )


def make_org() -> tuple[OrgStructure, str, str]:
    """标准组织：bootstrap 安装 root 岗位并建立 root 授予。"""
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    org = OrgStructure(team_id, owner)
    root_pos = Position(position_id=uuid.uuid4(), name="root", jd="团队负责人")
    org.bootstrap(
        [root_pos], root_agent_id="agent.root", root_position_id=root_pos.position_id
    )
    return org, "agent.root", owner


def pos(name: str, **kw) -> Position:
    return Position(position_id=uuid.uuid4(), name=name, **kw)


# ----------------------------------------------------------------------
# 验收 1：读改写岗位/边/授权 + POLICY_DENIED
# ----------------------------------------------------------------------


def test_org_structure_is_authority_subclass_with_registered_entities():
    org, root, _ = make_org()
    assert isinstance(org, Authority)
    # Authority 本身是 Device：自身实体并入注册中心（可被授予）。
    assert org.is_registered(org.org_manage_id)
    # root 岗位持 org_manage 授予 → root 可组织调整；陌生 agent 不可。
    assert org.authorize(root, org.org_manage_id).allowed
    assert not org.authorize(f"agent-{uuid.uuid4()}", org.org_manage_id).allowed


def test_privileged_agent_reads_writes_positions_edges_grants():
    org, root, _ = make_org()
    mgr = pos("manager", jd="管理调研")
    w1 = pos("worker1", jd="执行调研")
    w2 = pos("worker2", jd="写报告")
    org.add_position(mgr, by_agent_id=root)
    org.add_position(w1, by_agent_id=root)
    org.add_position(w2, by_agent_id=root)
    # 边读写（双向一致：superior_id ↔ subordinate_ids）
    org.set_superior(w1.position_id, mgr.position_id, by_agent_id=root)
    org.add_collaborator(w1.position_id, w2.position_id, by_agent_id=root)
    graph = org.graph(by_agent_id=root)
    assert graph.superior(w1.position_id).position_id == mgr.position_id
    # 边数据在岗位图副本上，比 id 不比 Position 对象（set_* 在副本上落库）
    assert [p.position_id for p in graph.subordinates(mgr.position_id)] == [
        w1.position_id
    ]
    assert [p.position_id for p in graph.collaborators(w1.position_id)] == [
        w2.position_id
    ]
    assert w1.position_id in org.get_position(
        mgr.position_id, by_agent_id=root
    ).subordinate_ids
    # JD 改写
    org.set_jd(mgr.position_id, "新 JD", by_agent_id=root)
    assert org.get_position(mgr.position_id, by_agent_id=root).jd == "新 JD"
    # 授权读写（Grant(position, entity)）
    dev = SampleDevice()
    dev.register_to(org)
    org.grant_org_capability(mgr.position_id, dev.tool_id, by_agent_id=root)
    projected = org.project_mount(mgr.position_id, by_agent_id=root)
    assert dev.tool_id in [g.entity_id for g in projected]
    org.revoke_org_capability(mgr.position_id, dev.tool_id, by_agent_id=root)
    assert dev.tool_id not in [
        g.entity_id for g in org.project_mount(mgr.position_id, by_agent_id=root)
    ]
    # 岗位清单只读视图
    names = {p.name for p in org.positions(by_agent_id=root).values()}
    assert names == {"root", "manager", "worker1", "worker2"}


def test_policy_denied_for_unprivileged_actor():
    org, root, _ = make_org()
    stranger = f"agent-{uuid.uuid4()}"
    p = pos("x")
    # 写：岗位/边/授权/mount
    with pytest.raises(OrgPermissionDenied, match="POLICY_DENIED"):
        org.add_position(p, by_agent_id=stranger)
    with pytest.raises(OrgPermissionDenied, match="POLICY_DENIED"):
        org.set_superior(p.position_id, None, by_agent_id=stranger)
    with pytest.raises(OrgPermissionDenied, match="POLICY_DENIED"):
        org.grant_org_capability(
            p.position_id, org.org_manage_id, by_agent_id=stranger
        )
    agent = Agent(agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default")
    with pytest.raises(OrgPermissionDenied, match="POLICY_DENIED"):
        org.mount(agent, p.position_id, by_agent_id=stranger)
    # 读：岗位清单/单岗位/图
    with pytest.raises(OrgPermissionDenied, match="POLICY_DENIED"):
        org.positions(by_agent_id=stranger)
    with pytest.raises(OrgPermissionDenied, match="POLICY_DENIED"):
        org.get_position(p.position_id, by_agent_id=stranger)
    with pytest.raises(OrgPermissionDenied, match="POLICY_DENIED"):
        org.graph(by_agent_id=stranger)


def test_unknown_position_rejected_on_org_ops():
    org, root, _ = make_org()
    missing = uuid.uuid4()
    with pytest.raises(Exception, match="不存在"):
        org.set_superior(missing, None, by_agent_id=root)
    with pytest.raises(Exception, match="不存在"):
        org.grant_org_capability(
            missing, org.org_manage_id, by_agent_id=root
        )
    with pytest.raises(Exception, match="不存在"):
        org.remove_position(missing, by_agent_id=root)


# ----------------------------------------------------------------------
# 验收 2：JD 与上下级关系作为 memory entry（priority<10 固定注入）
# ----------------------------------------------------------------------


def test_jd_and_relations_injected_as_fixed_working_memory():
    org, root, _ = make_org()
    mgr = pos("manager", jd="管理并审查产出")
    w1 = pos("worker1", jd="执行调研并写报告")
    org.add_position(mgr, by_agent_id=root)
    org.add_position(w1, by_agent_id=root)
    org.set_superior(w1.position_id, mgr.position_id, by_agent_id=root)
    agent = Agent(agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default")
    org.mount(agent, w1.position_id, by_agent_id=root)
    injections = org.injection_for(str(agent.agent_id))
    assert injections, "占据者应有记忆注入"
    by_tag = {i.source_tag: i for i in injections}
    # JD 注入（[POSITION_JD]，§5.8 org 干预杠杆）
    jd_inj = by_tag["[POSITION_JD]"]
    assert "执行调研并写报告" in jd_inj.content
    # 关系注入（[ORG_EDGE]：上级岗位）
    edge_inj = by_tag["[ORG_EDGE]"]
    assert "上级岗位" in edge_inj.content
    assert "manager" in edge_inj.content
    # 全部 priority<10 固定工作记忆（§4.3 分级）
    for i in injections:
        assert i.priority < PRIORITY_THRESHOLD
        assert priority_class(i.priority) is PriorityClass.FIXED_WORKING_MEMORY


def test_set_jd_and_edge_change_refresh_injected_content():
    org, root, _ = make_org()
    mgr = pos("manager", jd="旧 JD")
    w1 = pos("worker1")
    org.add_position(mgr, by_agent_id=root)
    org.add_position(w1, by_agent_id=root)
    agent = Agent(agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default")
    org.mount(agent, w1.position_id, by_agent_id=root)
    # 加边后占据者看到新关系
    org.set_superior(w1.position_id, mgr.position_id, by_agent_id=root)
    edge_inj = next(
        i for i in org.injection_for(str(agent.agent_id))
        if i.source_tag == "[ORG_EDGE]"
    )
    assert "manager" in edge_inj.content
    # 改 JD 后注入内容随改随新
    org.set_jd(mgr.position_id, "新 JD 内容", by_agent_id=root)
    mgr_agent = Agent(agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default")
    org.mount(mgr_agent, mgr.position_id, by_agent_id=root)
    jd_inj = next(
        i for i in org.injection_for(str(mgr_agent.agent_id))
        if i.source_tag == "[POSITION_JD]"
    )
    assert "新 JD 内容" in jd_inj.content


# ----------------------------------------------------------------------
# 验收 3：组织调整入 Journal 可审计；违反不变量被拒
# ----------------------------------------------------------------------


def test_org_changes_recorded_to_journal_sink():
    events: list[OrgChange] = []
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    org = OrgStructure(team_id, owner, journal_sink=events.append)
    root_pos = pos("root", jd="负责人")
    org.bootstrap(
        [root_pos], root_agent_id="agent.root", root_position_id=root_pos.position_id
    )
    worker = pos("worker")
    org.add_position(worker, by_agent_id="agent.root")
    org.set_superior(worker.position_id, root_pos.position_id, by_agent_id="agent.root")
    agent = Agent(agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default")
    org.mount(agent, worker.position_id, by_agent_id="agent.root")
    ops = [e.op for e in events]
    assert {"bootstrap", "add_position", "set_superior", "mount"} <= set(ops)
    assert all(e.actor_agent_id for e in events)  # 审计可归因
    mount_entry = next(e for e in events if e.op == "mount")
    assert mount_entry.position_id == str(worker.position_id)


def test_invariant_violation_rejected_and_not_journaled():
    events: list[OrgChange] = []
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    org = OrgStructure(team_id, owner, journal_sink=events.append)
    a, b, c = pos("a"), pos("b"), pos("c")
    org.bootstrap([a, b, c], root_agent_id="agent.root", root_position_id=a.position_id)
    org.set_superior(b.position_id, a.position_id, by_agent_id="agent.root")
    org.set_superior(c.position_id, b.position_id, by_agent_id="agent.root")
    n_before = len(events)
    # a 的上级设为 c → 委派边成环 a→b→c→a（委派单调，§5.8/§11）
    with pytest.raises(GovernanceInvariantError, match="委派单调"):
        org.set_superior(a.position_id, c.position_id, by_agent_id="agent.root")
    assert len(events) == n_before  # 被拒操作不入 Journal
    # 状态未变：a 仍无上级
    assert org.get_position(a.position_id, by_agent_id="agent.root").superior_id is None


def test_dangling_edge_rejected_on_add_position():
    org, root, _ = make_org()
    missing = uuid.uuid4()
    p = Position(
        position_id=uuid.uuid4(), name="x", subordinate_ids=[missing]
    )
    with pytest.raises(GovernanceInvariantError, match="不存在"):
        org.add_position(p, by_agent_id=root)
    # 岗位未落库
    assert org.get_position(p.position_id, by_agent_id=root) is None


# ----------------------------------------------------------------------
# 验收 4：设备注册 UI 插件后 Control Plane 渲染对应模块
# ----------------------------------------------------------------------


def test_ui_plugin_registration_and_render():
    org, root, _ = make_org()
    plane = ControlPlane(runtime=None)  # 纯框架：UI 注册/渲染不依赖运行态
    plane.register_device_ui(org)
    manifest = plane.ui_manifest()
    names = {m["module_name"] for m in manifest}
    assert {"org.positions", "org.mount"} <= names
    rendered = plane.render_ui_modules()
    panel = rendered["org.positions"]
    assert panel["frontend_module"] == "org/positions-panel"
    assert panel["device_id"] == org.device_id
    # 后端 handler 渲染岗位数据（§10 GET /org/positions 的插件视图）
    assert any(p["name"] == "root" for p in panel["data"]["positions"])
    assert "edge_semantics" in panel["data"]
    # 重复注册拒绝
    with pytest.raises(ValueError, match="已注册"):
        plane.register_device_ui(org)


def test_ui_plugin_registry_manifest_is_deterministic():
    org, root, _ = make_org()
    registry = ControlPlane(runtime=None).ui_registry
    registry.register_device(org)
    modules = registry.modules()
    assert set(modules) == {"org.positions", "org.mount"}
    assert modules["org.positions"][0] == org.device_id  # (device_id, module)


# ----------------------------------------------------------------------
# 验收 5：mount（岗人分离）静态可用；动态评估接口预留
# ----------------------------------------------------------------------


def test_mount_assigns_position_and_grants_membership():
    org, root, _ = make_org()
    worker = pos("worker", jd="执行调研")
    org.add_position(worker, by_agent_id=root)
    agent = Agent(agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default")
    assignment = org.mount(agent, worker.position_id, by_agent_id=root)
    # N2 DirectAssignment：agent → position 直派记录
    assert assignment.agent_id == agent.agent_id
    assert assignment.position_id == worker.position_id
    # position_ref 落位（占据即继承）
    assert agent.position_ref == worker.position_id
    # 两层 Grant：给岗位授予工具 → 占据者可用；org_manage 不继承
    dev = SampleDevice()
    dev.register_to(org)
    org.grant_org_capability(worker.position_id, dev.tool_id, by_agent_id=root)
    assert org.authorize(str(agent.agent_id), dev.tool_id).allowed
    assert org.authorize(str(agent.agent_id), org.org_manage_id).effect is (
        GrantEffect.DENIED
    )
    # 动态评估接口预留：project_mount = 挂载将继承的能力授予
    # （含岗位的 JD/关系记忆实体授予，N4 预算层可滤）
    projected = [
        g.entity_id for g in org.project_mount(worker.position_id, by_agent_id=root)
    ]
    assert dev.tool_id in projected
    assert org.org_manage_id not in projected  # org_manage 不随岗位继承
    # 解除挂载
    org.unmount(str(agent.agent_id), worker.position_id, by_agent_id=root)
    assert not org.authorize(str(agent.agent_id), dev.tool_id).allowed


def test_mount_multi_variant_candidates():
    """多版本 agent 候选可挂载同一岗位（§4.1 岗人分离，静态先行）。"""
    org, root, _ = make_org()
    worker = pos("worker", jd="执行调研")
    org.add_position(worker, by_agent_id=root)
    v1 = Agent(
        agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default",
        metadata={"variant": "v1"},
    )
    v2 = Agent(
        agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default",
        metadata={"variant": "v2"},
    )
    org.mount(v1, worker.position_id, by_agent_id=root)
    org.mount(v2, worker.position_id, by_agent_id=root)
    # 布线中心：两个候选同岗（Grant(agent, position) 两层中的成员层）
    assert str(worker.position_id) in org._memberships.get(str(v1.agent_id), set())
    assert str(worker.position_id) in org._memberships.get(str(v2.agent_id), set())


# ----------------------------------------------------------------------
# 验收 6：直派 Authority 可替换组织架构（同一接口）
# ----------------------------------------------------------------------


def test_direct_authority_replaceable_by_org_structure():
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    plain = Authority(team_id, owner)
    dev = SampleDevice()
    dev.register_to(plain)
    # 朴素系统直派：不经组织架构直接指派 position（§5.8 直派形态）
    plain.grant_membership("agent.1", "pos-1")
    plain.grant_capability("pos-1", dev.tool_id)
    # 替换候选（register=False，不进 Team 注册表）
    org_candidate = OrgStructure(team_id, owner, register=False)
    plain.replace(org_candidate, by_agent_id=owner)
    assert authority_for(team_id) is org_candidate
    # 布线移交：原授予在新组织架构上仍生效（直派无缝升级）
    assert org_candidate.authorize("agent.1", dev.tool_id).effect is (
        GrantEffect.ALLOWED
    )
    # 组织架构自身实体已并入注册中心：org_manage 可被授予
    org_candidate.grant_capability("pos-1", org_candidate.org_manage_id, priority=1)
    org_candidate.grant_membership("agent.root", "pos-1")
    assert org_candidate.authorize("agent.root", org_candidate.org_manage_id).allowed
    # 单例强制持续生效
    with pytest.raises(DuplicateAuthorityError):
        Authority(team_id, owner)


def test_replace_requires_owner():
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    plain = Authority(team_id, owner)
    org_candidate = OrgStructure(team_id, owner, register=False)
    with pytest.raises(NotOwnerError, match="owner"):
        plain.replace(org_candidate, by_agent_id=f"agent-{uuid.uuid4()}")


def test_org_to_org_replace_transfers_positions():
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    org1 = OrgStructure(team_id, owner)
    root_pos = pos("root", jd="负责人")
    org1.bootstrap(
        [root_pos], root_agent_id="agent.root", root_position_id=root_pos.position_id
    )
    org2 = OrgStructure(team_id, owner, register=False)
    org1.replace(org2, by_agent_id=owner)
    assert authority_for(team_id) is org2
    # 岗位数据面移交 + root 授予延续
    assert org2.get_position(root_pos.position_id, by_agent_id="agent.root") is not None
    assert org2.authorize("agent.root", org2.org_manage_id).allowed
    # 占据者记忆注入持续生效（新实例重新注册 JD/关系实体，无双份）
    agent = Agent(agent_id=uuid.uuid4(), kind=AgentKind.LLM, llm_profile="default")
    org2.mount(agent, root_pos.position_id, by_agent_id="agent.root")
    tags = [i.source_tag for i in org2.injection_for(str(agent.agent_id))]
    assert tags.count("[POSITION_JD]") == 1
    assert tags.count("[ORG_EDGE]") == 1
