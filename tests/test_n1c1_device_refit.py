"""N1c-1: 设备适配层 — store 归位为 Device 子类验收测试
（SPEC §5.1–5.6，N1c 设备适配层）。

覆盖卡面验收：
- SharedKB / RecordStore / AssetStore / CredentialStore / MailSystem
  均继承 Device，可直接当 Device 使用；
- 每个 store 构造时注册受控 uuid（DATA 范围 + TOOL 采 uuid5 adopt）；
- register_to(authority) 可把受控 uuid 提交到 Authority 注册中心；
- InjectionDecl 声明了注入内容（source_tag 存在且内容非空）；
- 工具 capability uuid = ToolManifest._derive_capability()（uuid5 派生，
  跨实例稳定）；
- adopt 机制（显式 entity_id）注册到 Authority 后 is_registered 为 True；
- 构造签名兼容：无需新参数即可构造。
"""

from __future__ import annotations

import uuid

import pytest

from my_team.asset_store import AssetStore
from my_team.credential_store import CredentialStore
from my_team.devices import (
    Authority,
    EntityKind,
    GrantEffect,
    InjectionDecl,
    new_team_id,
)
from my_team.devices.base import Device
from my_team.mailbox import MailSystem
from my_team.record_store import RecordStore
from my_team.shared_kb import SharedKB
from my_team.tool_manifest import builtin_manifests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_authority() -> Authority:
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    return Authority(team_id, owner)


# ---------------------------------------------------------------------------
# 1. 继承检查：每个 store 是 Device 子类
# ---------------------------------------------------------------------------


def test_shared_kb_is_device():
    kb = SharedKB()
    assert isinstance(kb, Device)
    assert isinstance(kb, SharedKB)


def test_record_store_is_device():
    rs = RecordStore()
    assert isinstance(rs, Device)
    assert isinstance(rs, RecordStore)


def test_asset_store_is_device():
    aso = AssetStore()
    assert isinstance(aso, Device)
    assert isinstance(aso, AssetStore)


def test_credential_store_is_device():
    cs = CredentialStore()
    assert isinstance(cs, Device)
    assert isinstance(cs, CredentialStore)


def test_mail_system_is_device():
    ms = MailSystem()
    assert isinstance(ms, Device)
    assert isinstance(ms, MailSystem)


# ---------------------------------------------------------------------------
# 2. 构造签名兼容：原始方式无额外参数仍可构造
# ---------------------------------------------------------------------------


def test_shared_kb_compat_construction():
    """simulation.py 构造方式保持兼容。"""
    from my_team.shared_kb import (
        LockManager,
        PermissionEngine,
        VersionControl,
    )
    permissions = PermissionEngine()
    lock_manager = LockManager()
    version_control = VersionControl()
    kb = SharedKB(
        permissions=permissions,
        lock_manager=lock_manager,
        version_control=version_control,
    )
    assert isinstance(kb, Device)
    assert kb._permissions is permissions
    assert kb._locks is lock_manager
    assert kb._versions is version_control


def test_record_store_compat_construction():
    rs = RecordStore()
    assert isinstance(rs, Device)
    assert rs._ledger == []


def test_asset_store_compat_construction():
    aso = AssetStore()
    assert isinstance(aso, Device)
    assert len(aso) == 0


def test_credential_store_compat_construction():
    cs = CredentialStore()
    assert isinstance(cs, Device)
    assert cs.default_backend is None


def test_mail_system_compat_construction():
    ms = MailSystem()
    assert isinstance(ms, Device)
    assert ms.pending_count == 0


# ---------------------------------------------------------------------------
# 3. 受控实体已注册（构造后 entities 非空）
# ---------------------------------------------------------------------------


def test_shared_kb_has_registered_entities():
    kb = SharedKB()
    assert len(kb.entities) >= 1  # 至少有 DATA scope
    # 工具实体（kb_read/kb_write/kb_list/kb_search）
    entity_labels = {e.label for e in kb.entities.values()}
    assert "kb_read" in entity_labels
    assert "kb_write" in entity_labels
    assert "kb_list" in entity_labels
    assert "kb_search" in entity_labels
    assert "shared-kb-scope" in entity_labels


def test_record_store_has_registered_entities():
    rs = RecordStore()
    entity_labels = {e.label for e in rs.entities.values()}
    assert "record-store-scope" in entity_labels
    assert "record_upsert" in entity_labels
    assert "record_delta" in entity_labels


def test_asset_store_has_registered_entities():
    aso = AssetStore()
    entity_labels = {e.label for e in aso.entities.values()}
    assert "asset-store-scope" in entity_labels


def test_credential_store_has_registered_entities():
    cs = CredentialStore()
    entity_labels = {e.label for e in cs.entities.values()}
    assert "credential-store-scope" in entity_labels


def test_mail_system_has_registered_entities():
    ms = MailSystem()
    entity_labels = {e.label for e in ms.entities.values()}
    assert "mail-system-scope" in entity_labels
    assert "send_email" in entity_labels


# ---------------------------------------------------------------------------
# 4. InjectionDecl 声明正确（scope DATA 实体有注入内容）
# ---------------------------------------------------------------------------


def test_shared_kb_injection_decl():
    kb = SharedKB()
    scope_entity = kb.entities[kb.kb_scope_id]
    assert scope_entity.injection is not None
    assert scope_entity.injection.content
    assert scope_entity.injection.source_tag  # non-empty tag


def test_record_store_injection_decl():
    rs = RecordStore()
    scope_entity = rs.entities[rs.records_scope_id]
    assert scope_entity.injection is not None
    assert scope_entity.injection.content
    assert scope_entity.injection.source_tag


def test_asset_store_injection_decl():
    aso = AssetStore()
    scope_entity = aso.entities[aso.assets_scope_id]
    assert scope_entity.injection is not None
    assert scope_entity.injection.content
    assert scope_entity.injection.source_tag


def test_credential_store_injection_decl():
    cs = CredentialStore()
    scope_entity = cs.entities[cs.credentials_scope_id]
    assert scope_entity.injection is not None
    assert scope_entity.injection.content
    assert scope_entity.injection.source_tag


def test_mail_system_injection_decl():
    ms = MailSystem()
    scope_entity = ms.entities[ms.mail_scope_id]
    assert scope_entity.injection is not None
    assert scope_entity.injection.content
    assert scope_entity.injection.source_tag


# ---------------------------------------------------------------------------
# 5. 工具 capability = uuid5 派生值，跨实例稳定（adopt 机制）
# ---------------------------------------------------------------------------


def _get_manifests():
    return builtin_manifests()


def test_shared_kb_tool_capabilities_adopt_uuid5():
    """KB 工具 capability 采 uuid5，跨实例一致。"""
    kb1 = SharedKB()
    kb2 = SharedKB()
    manifests = _get_manifests()
    # kb_read capability 跨实例一致（uuid5 派生）
    assert kb1.kb_read_capability == manifests["kb_read"].capability
    assert kb2.kb_read_capability == kb1.kb_read_capability
    # 且是合法 uuid
    uuid.UUID(kb1.kb_read_capability)


def test_record_store_tool_capabilities_adopt_uuid5():
    rs1 = RecordStore()
    rs2 = RecordStore()
    manifests = _get_manifests()
    assert rs1.record_upsert_capability == manifests["record_upsert"].capability
    assert rs2.record_upsert_capability == rs1.record_upsert_capability
    uuid.UUID(rs1.record_upsert_capability)


def test_mail_system_tool_capability_adopts_uuid5():
    ms1 = MailSystem()
    ms2 = MailSystem()
    manifests = _get_manifests()
    assert ms1.send_email_capability == manifests["send_email"].capability
    assert ms2.send_email_capability == ms1.send_email_capability
    uuid.UUID(ms1.send_email_capability)


# ---------------------------------------------------------------------------
# 6. register_to(authority) — 受控 uuid 进入 Authority 注册中心
# ---------------------------------------------------------------------------


def test_shared_kb_register_to_authority():
    auth = make_authority()
    kb = SharedKB()
    kb.register_to(auth)
    # 所有实体均已注册
    for eid in kb.entities:
        assert auth.is_registered(eid), f"entity {eid!r} not registered"
    # device_id 正确
    assert auth.registered[kb.kb_scope_id].device_id == kb.device_id


def test_record_store_register_to_authority():
    auth = make_authority()
    rs = RecordStore()
    rs.register_to(auth)
    for eid in rs.entities:
        assert auth.is_registered(eid)


def test_asset_store_register_to_authority():
    auth = make_authority()
    aso = AssetStore()
    aso.register_to(auth)
    for eid in aso.entities:
        assert auth.is_registered(eid)


def test_credential_store_register_to_authority():
    auth = make_authority()
    cs = CredentialStore()
    cs.register_to(auth)
    for eid in cs.entities:
        assert auth.is_registered(eid)


def test_mail_system_register_to_authority():
    auth = make_authority()
    ms = MailSystem()
    ms.register_to(auth)
    for eid in ms.entities:
        assert auth.is_registered(eid)
    # send_email capability 可以被授予
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", ms.send_email_capability)
    d = auth.authorize("agent.1", ms.send_email_capability)
    assert d.effect is GrantEffect.ALLOWED


# ---------------------------------------------------------------------------
# 7. Authority.injection_for 可从注册后的 store 实体收集注入内容
# ---------------------------------------------------------------------------


def test_shared_kb_injection_flows_through_authority():
    auth = make_authority()
    kb = SharedKB()
    kb.register_to(auth)
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", kb.kb_scope_id, priority=5)
    injections = auth.injection_for("agent.1")
    assert any(
        inj.entity_id == kb.kb_scope_id
        and "[KB_INSTRUCTION]" in inj.source_tag
        for inj in injections
    ), "KB InjectionDecl not found in injections"


def test_mail_system_injection_flows_through_authority():
    auth = make_authority()
    ms = MailSystem()
    ms.register_to(auth)
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", ms.mail_scope_id, priority=5)
    injections = auth.injection_for("agent.1")
    assert any(
        inj.entity_id == ms.mail_scope_id
        and "[MAIL_INSTRUCTION]" in inj.source_tag
        for inj in injections
    ), "Mail InjectionDecl not found in injections"


def test_record_store_injection_flows_through_authority():
    auth = make_authority()
    rs = RecordStore()
    rs.register_to(auth)
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", rs.records_scope_id, priority=5)
    injections = auth.injection_for("agent.1")
    assert any(
        inj.entity_id == rs.records_scope_id for inj in injections
    )


# ---------------------------------------------------------------------------
# 8. adopt 机制：register_entity(entity_id=...) 使用显式 uuid
# ---------------------------------------------------------------------------


def test_base_device_adopt_entity_id():
    """base.py register_entity 支持显式 entity_id（adopt 机制）。"""
    from my_team.devices.base import Device

    class _TDev(Device):
        pass

    dev = _TDev()
    explicit_id = str(uuid.uuid4())
    returned_id = dev.register_entity(EntityKind.DATA, "x", entity_id=explicit_id)
    assert returned_id == explicit_id
    assert explicit_id in dev.entities
    assert dev.entities[explicit_id].entity_id == explicit_id


def test_adopt_uuid5_matches_manifest_capability():
    """采 uuid5 派生值后，注册的 entity_id 与 manifest.capability 一致。"""
    manifests = builtin_manifests()
    kb = SharedKB()
    # kb_write_capability 是 adopt 来的 uuid5
    assert kb.kb_write_capability == manifests["kb_write"].capability
    # 且是合法 uuid（uuid5 版本检查）
    parsed = uuid.UUID(kb.kb_write_capability)
    assert parsed.version == 5  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 9. 业务功能不变（smoke test — store 仍可正常使用）
# ---------------------------------------------------------------------------


def test_shared_kb_business_still_works():
    from my_team.shared_kb import PermissionEngine, PermissionRule
    permissions = PermissionEngine()
    permissions.add_rule(PermissionRule(scope="*", principal="agent.1", allow=["list", "read", "create", "write"]))
    kb = SharedKB(permissions=permissions)
    kb.create("notes/hello.md", "agent.1", content="hello", tick=1)
    res = kb.read("notes/hello.md", "agent.1")
    assert res.content == "hello"


def test_record_store_business_still_works():
    from my_team.record_store import RecordSchema, FieldSpec
    rs = RecordStore()
    schema = RecordSchema(
        record_type="item",
        fields=[FieldSpec(name="name", type="string")],
    )
    rs.register_schema(schema)
    result = rs.upsert("item", "k1", {"name": "hello"}, agent_id="agent.1", tick=1)
    assert result.record["name"] == "hello"


def test_asset_store_business_still_works():
    aso = AssetStore()
    meta = aso.put(b"hello world", mime="text/plain", tick=1)
    data = aso.get(meta.sha256)
    assert data == b"hello world"


def test_mail_system_business_still_works():
    ms = MailSystem()
    ms.register_agent("agent.1")
    ms.register_agent("agent.2")
    email = ms.create_email(
        from_agent="agent.1",
        to=["agent.2"],
        subject="Test",
        body="Hello",
        tick=1,
    )
    delivered = ms.deliver(current_tick=2)
    assert len(delivered) == 1
    mailbox = ms.get_mailbox("agent.2")
    assert mailbox is not None
    assert len(mailbox.inbox) == 1
