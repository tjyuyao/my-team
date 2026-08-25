"""N1a: 设备协议 — Device 一般结构（SPEC §5.1，KANBAN/TODO/2026-08-24-
device-protocol-authority.md）。

覆盖卡面验收（设备协议侧）：
- 设备注册受控 uuid（数据条目/范围、工具/工具包）；未注册前对
  Authority 不可见；
- 设备经 register_to 把全部受控 uuid 提交到 Authority 注册中心；
- 注册即声明注入内容（InjectionDecl，设备协议三条之三）。
"""

from __future__ import annotations

import uuid

import pytest

from my_team.devices import (
    Authority,
    Device,
    EntityKind,
    InjectionDecl,
    new_entity_id,
    new_team_id,
)


class SampleDevice(Device):
    """最小设备实现（数据面占位，仅验证协议骨架）。"""


def make_team() -> tuple[str, Authority, str]:
    """返回 (team_id, authority, owner_agent_id)。"""
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    return team_id, Authority(team_id, owner), owner


def test_register_entity_assigns_uuid4():
    dev = SampleDevice()
    eid = dev.register_entity(EntityKind.DATA, "kb-page-1")
    assert isinstance(uuid.UUID(eid), uuid.UUID)
    ent = dev.entities[eid]
    assert ent.device_id == dev.device_id
    assert ent.kind is EntityKind.DATA
    assert ent.label == "kb-page-1"
    assert ent.injection is None


def test_register_entity_kinds():
    dev = SampleDevice()
    data = dev.register_entity(EntityKind.DATA, "record-family")
    tool = dev.register_entity(EntityKind.TOOL, "kb_read")
    pack = dev.register_entity(EntityKind.TOOLPACK, "mail-tools")
    assert dev.entities[data].kind is EntityKind.DATA
    assert dev.entities[tool].kind is EntityKind.TOOL
    assert dev.entities[pack].kind is EntityKind.TOOLPACK


def test_register_to_submits_all_entities_to_authority():
    _, authority, _ = make_team()
    dev = SampleDevice()
    e1 = dev.register_entity(EntityKind.DATA, "page-a")
    e2 = dev.register_entity(EntityKind.TOOL, "kb_read")
    # 未注册前 Authority 不可见
    assert not authority.is_registered(e1)
    dev.register_to(authority)
    assert authority.is_registered(e1)
    assert authority.is_registered(e2)
    assert authority.registered[e1].device_id == dev.device_id


def test_injection_decl_on_entity():
    dev = SampleDevice()
    eid = dev.register_entity(
        EntityKind.DATA,
        "kb-page-permission",
        injection=InjectionDecl(
            content="运营知识页：可读；条目级权限说明见本声明",
            source_tag="[KB_GUIDE]",
        ),
    )
    decl = dev.injected_content_for(eid)
    assert decl is not None
    assert decl.source_tag == "[KB_GUIDE]"
    # 未声明注入的实体返回 None
    plain = dev.register_entity(EntityKind.TOOL, "kb_search")
    assert dev.injected_content_for(plain) is None


def test_entities_view_is_read_only():
    dev = SampleDevice()
    dev.register_entity(EntityKind.DATA, "x")
    with pytest.raises(TypeError):
        dev.entities["new"] = "nope"  # type: ignore[index]


def test_new_entity_id_is_uuid4():
    assert isinstance(uuid.UUID(new_entity_id()), uuid.UUID)
