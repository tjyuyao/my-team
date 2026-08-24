"""N1a: Authority — 注册中心 + 布线中心 + 两层 Grant + 单例/Owner
（SPEC §5.1，KANBAN/TODO/2026-08-24-device-protocol-authority.md）。

覆盖卡面验收（Authority 侧）：
- 设备注册受控 uuid 至 Authority；未注册 uuid 无法被授予；
- 任一调用 = ∃position：Grant(agent, position) ∧ Grant(position,
  entity)，deny-by-default（求值路径测试）；
- Authority 每 Team 单例强制；安装/替换仅 Owner；
- 注册即声明注入内容（content 声明 + 注入接线接口 = 能力=权限+记忆，
  N4 联测钩子）。
"""

from __future__ import annotations

import uuid

import pytest

from my_team.devices import (
    Authority,
    ConfigDevice,
    DuplicateAuthorityError,
    EntityKind,
    GrantEffect,
    InjectionDecl,
    NotOwnerError,
    UnknownEntityError,
    new_team_id,
)
from my_team.devices.base import Device


class SampleDevice(Device):
    """带受控实体与注入声明的最小设备。"""

    def __init__(self, device_id: str | None = None) -> None:
        super().__init__(device_id)
        self.page_id = self.register_entity(
            EntityKind.DATA, "kb-page",
            injection=InjectionDecl(
                content="页面权限说明：谁可读写本页",
                source_tag="[KB_GUIDE]",
            ),
        )
        self.tool_id = self.register_entity(EntityKind.TOOL, "kb_read")
        self.secret_id = self.register_entity(
            EntityKind.DATA, "secret-records",
            injection=InjectionDecl(
                content="机密记录访问说明", source_tag="[KB_GUIDE]"
            ),
        )


def make_team() -> tuple[str, Authority, str]:
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    return team_id, Authority(team_id, owner), owner


# ----------------------------------------------------------------------
# 单例强制 + Owner 安装/替换
# ----------------------------------------------------------------------


def test_singleton_per_team():
    team_id, _, _ = make_team()
    with pytest.raises(DuplicateAuthorityError):
        Authority(team_id, f"agent-{uuid.uuid4()}")


def test_authority_for_returns_team_authority():
    team_id, auth, _ = make_team()
    from my_team.devices import authority_for

    assert authority_for(team_id) is auth
    assert authority_for(new_team_id()) is None


def test_replace_requires_owner():
    team_id, auth, owner = make_team()
    stranger = f"agent-{uuid.uuid4()}"
    new_auth = Authority(team_id, owner, register=False)  # 替换候选
    # 非 Owner 替换被拒
    with pytest.raises(NotOwnerError):
        auth.replace(new_auth, by_agent_id=stranger)
    # Owner 替换成功：数据移交 + 注册表指向新实例
    auth.replace(new_auth, by_agent_id=owner)
    from my_team.devices import authority_for

    assert authority_for(team_id) is new_auth


def test_replace_keeps_wiring():
    team_id, auth, owner = make_team()
    dev = SampleDevice()
    dev.register_to(auth)
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", dev.tool_id)
    new_auth = Authority(team_id, owner, register=False)
    auth.replace(new_auth, by_agent_id=owner)
    # 布线数据移交：原授予在新 Authority 上仍生效
    assert new_auth.authorize("agent.1", dev.tool_id).effect is (
        GrantEffect.ALLOWED
    )


# ----------------------------------------------------------------------
# 注册中心 + 未注册 uuid 不可被授予
# ----------------------------------------------------------------------


def test_unknown_entity_cannot_be_granted():
    _, auth, _ = make_team()
    with pytest.raises(UnknownEntityError):
        auth.grant_capability("pos-1", str(uuid.uuid4()))


def test_unregistered_entity_denied():
    _, auth, _ = make_team()
    # 未注册 uuid：即使有成员关系也 deny（deny-by-default）
    auth.grant_membership("agent.1", "pos-1")
    d = auth.authorize("agent.1", str(uuid.uuid4()))
    assert d.effect is GrantEffect.DENIED
    assert not d.allowed


def test_authority_is_device_and_can_register_self():
    _, auth, _ = make_team()
    eid = auth.register_self(label="authority-meta")
    assert auth.is_registered(eid)
    assert auth.registered[eid].device_id == auth.device_id


# ----------------------------------------------------------------------
# 两层 Grant 求值（deny-by-default）
# ----------------------------------------------------------------------


def _wired() -> tuple[Authority, SampleDevice]:
    _, auth, _ = make_team()
    dev = SampleDevice()
    dev.register_to(auth)
    return auth, dev


def test_deny_by_default_no_membership_no_capability():
    auth, dev = _wired()
    d = auth.authorize("agent.1", dev.tool_id)
    assert d.effect is GrantEffect.DENIED
    assert not d.allowed


def test_two_layer_grant_allowed():
    auth, dev = _wired()
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", dev.tool_id)
    d = auth.authorize("agent.1", dev.tool_id)
    assert d.effect is GrantEffect.ALLOWED
    assert d.allowed
    assert d.position_id == "pos-1"


def test_membership_alone_denied():
    auth, dev = _wired()
    auth.grant_membership("agent.1", "pos-1")  # 有成员无能力
    d = auth.authorize("agent.1", dev.tool_id)
    assert d.effect is GrantEffect.DENIED


def test_capability_without_membership_denied():
    auth, dev = _wired()
    auth.grant_capability("pos-1", dev.tool_id)  # 有能力无成员
    d = auth.authorize("agent.1", dev.tool_id)
    assert d.effect is GrantEffect.DENIED


def test_denied_overrides_allowed():
    auth, dev = _wired()
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_membership("agent.1", "pos-2")
    auth.grant_capability("pos-1", dev.tool_id)  # allowed
    auth.grant_capability(
        "pos-2", dev.tool_id, effect=GrantEffect.DENIED
    )
    d = auth.authorize("agent.1", dev.tool_id)
    assert d.effect is GrantEffect.DENIED  # 显式 denied 优先（安全）
    assert not d.allowed


def test_requires_approval_fallback():
    auth, dev = _wired()
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability(
        "pos-1", dev.tool_id, effect=GrantEffect.REQUIRES_APPROVAL
    )
    d = auth.authorize("agent.1", dev.tool_id)
    assert d.effect is GrantEffect.REQUIRES_APPROVAL
    assert d.allowed  # 有授予即进效果路径（审批是另一层，§3.5 三查）


def test_revoke_membership_and_capability():
    auth, dev = _wired()
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", dev.tool_id)
    assert auth.authorize("agent.1", dev.tool_id).allowed
    auth.revoke_capability("pos-1", dev.tool_id)
    assert not auth.authorize("agent.1", dev.tool_id).allowed
    auth.grant_capability("pos-1", dev.tool_id)
    auth.revoke_membership("agent.1", "pos-1")
    assert not auth.authorize("agent.1", dev.tool_id).allowed


# ----------------------------------------------------------------------
# 能力 = 权限 + 记忆：注入接线（N4 联测钩子）
# ----------------------------------------------------------------------


def test_injection_for_collects_declared_content():
    auth, dev = _wired()
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", dev.page_id, priority=5)
    auth.grant_capability("pos-1", dev.tool_id, priority=12)  # 无注入声明
    injections = auth.injection_for("agent.1")
    assert len(injections) == 1  # 只收集声明了注入内容的实体
    inj = injections[0]
    assert inj.entity_id == dev.page_id
    assert inj.source_device_id == dev.device_id
    assert inj.position_id == "pos-1"
    assert inj.priority == 5  # priority 来自授予
    assert "[KB_GUIDE]" in inj.source_tag


def test_injection_requires_membership():
    auth, dev = _wired()
    auth.grant_capability("pos-1", dev.page_id)  # 无成员
    assert auth.injection_for("agent.1") == []


def test_injection_priority_from_grant():
    auth, dev = _wired()
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", dev.page_id, priority=2)  # <10 固定
    inj = auth.injection_for("agent.1")[0]
    assert inj.priority < 10
    auth.grant_capability("pos-1", dev.secret_id, priority=20)  # ≥10 召回
    priorities = {i.entity_id: i.priority for i in auth.injection_for("agent.1")}
    assert priorities[dev.secret_id] == 20


# ----------------------------------------------------------------------
# 配置设备：初始授予集 → Authority（引导）
# ----------------------------------------------------------------------


def test_config_device_apply_to_bootstraps_grants():
    _, auth, _ = make_team()
    dev = SampleDevice()
    dev.register_to(auth)
    cfg = ConfigDevice()
    cfg.add_membership("agent.1", "pos-1")
    cfg.add_capability("pos-1", dev.tool_id, priority=5)
    cfg.apply_to(auth)
    d = auth.authorize("agent.1", dev.tool_id)
    assert d.effect is GrantEffect.ALLOWED
    assert d.position_id == "pos-1"


def test_config_device_apply_to_rejects_unregistered():
    _, auth, _ = make_team()
    cfg = ConfigDevice()
    cfg.add_capability("pos-1", str(uuid.uuid4()))  # 未注册
    with pytest.raises(UnknownEntityError):
        cfg.apply_to(auth)


def test_config_device_defaults():
    cfg = ConfigDevice()
    assert cfg.limits.max_active_agents_per_tick >= 1
    assert cfg.memory_budget.fixed_memory_tokens > 0
    assert isinstance(cfg.device_id, str)
    assert cfg.allowlist == set()
