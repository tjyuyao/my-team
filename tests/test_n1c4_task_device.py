"""N1c-4: Task 设备公共数据层 — TaskTree 归位为 Device 子类验收测试
（SPEC §5.7，N1c-4 Task 设备公共数据层）。

覆盖卡面验收：
- TaskTree 继承 Device，可直接当 Device 使用；
- 构造时注册受控 uuid（DATA task-tree-scope + TOOL delegate，adopt uuid5）；
- register_to(authority) 可把受控 uuid 提交到 Authority 注册中心；
- InjectionDecl 声明了注入内容（source_tag 非空）；
- delegate capability uuid = ToolManifest._derive_capability() 跨实例稳定；
- make_handle_delegate 在设备化后仍工作（注入链完整）；
- TaskTree 业务功能（CRUD/状态机/回滚语义）完全不变。

"""

from __future__ import annotations

import uuid

from my_team.devices import (
    Authority,
    EntityKind,
    GrantEffect,
    new_team_id,
)
from my_team.devices.base import Device
from my_team.task_tree import TaskTree
from my_team.tool_manifest import builtin_manifests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_authority() -> Authority:
    team_id = new_team_id()
    owner = f"agent-{uuid.uuid4()}"
    return Authority(team_id, owner)


# ---------------------------------------------------------------------------
# 1. 继承检查：TaskTree 是 Device 子类
# ---------------------------------------------------------------------------


def test_task_tree_is_device():
    tt = TaskTree()
    assert isinstance(tt, Device)
    assert isinstance(tt, TaskTree)


# ---------------------------------------------------------------------------
# 2. 构造签名兼容：无参构造 + 位置参数 transaction_buffer + device_id
# ---------------------------------------------------------------------------


def test_task_tree_compat_no_args():
    """simulation.py 无参构造方式保持兼容。"""
    tt = TaskTree()
    assert isinstance(tt, Device)
    assert tt._transaction_buffer is None


def test_task_tree_compat_with_transaction_buffer():
    """位置参数 transaction_buffer 仍可正常传入。"""
    # 只检查构造不报错、属性绑定正确（TransactionBuffer 接口不在本测试范围）
    # 用 None 代替真实 buffer，符合 simulation.py 先例。
    tt = TaskTree(transaction_buffer=None)
    assert tt._transaction_buffer is None


def test_task_tree_compat_with_device_id():
    """显式 device_id 可传入（simulation 注册路径会用到）。"""
    dev_id = str(uuid.uuid4())
    tt = TaskTree(device_id=dev_id)
    assert tt.device_id == dev_id


# ---------------------------------------------------------------------------
# 3. 受控实体已注册（构造后 entities 非空）
# ---------------------------------------------------------------------------


def test_task_tree_has_registered_entities():
    tt = TaskTree()
    entity_labels = {e.label for e in tt.entities.values()}
    assert "task-tree-scope" in entity_labels
    assert "delegate" in entity_labels


def test_task_tree_entity_kinds():
    tt = TaskTree()
    kinds = {e.label: e.kind for e in tt.entities.values()}
    assert kinds["task-tree-scope"] == EntityKind.DATA
    assert kinds["delegate"] == EntityKind.TOOL


# ---------------------------------------------------------------------------
# 4. InjectionDecl 声明正确（scope DATA 实体有注入内容）
# ---------------------------------------------------------------------------


def test_task_tree_injection_decl():
    tt = TaskTree()
    scope_entity = tt.entities[tt.task_tree_scope_id]
    assert scope_entity.injection is not None
    assert scope_entity.injection.content
    assert scope_entity.injection.source_tag
    assert "[TASK_INSTRUCTION]" in scope_entity.injection.source_tag


def test_task_tree_injection_content_mentions_delegate():
    tt = TaskTree()
    scope_entity = tt.entities[tt.task_tree_scope_id]
    assert scope_entity.injection is not None
    assert "delegate" in scope_entity.injection.content.lower()


# ---------------------------------------------------------------------------
# 5. delegate capability = uuid5 派生值，跨实例稳定（adopt 机制）
# ---------------------------------------------------------------------------


def test_delegate_capability_adopts_uuid5():
    """delegate TOOL capability 采 uuid5，跨实例一致。"""
    tt1 = TaskTree()
    tt2 = TaskTree()
    manifests = builtin_manifests()
    assert tt1.delegate_capability == manifests["delegate"].capability
    assert tt2.delegate_capability == tt1.delegate_capability
    # 合法 uuid
    parsed = uuid.UUID(tt1.delegate_capability)
    assert parsed.version == 5  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 6. register_to(authority) — 受控 uuid 进入 Authority 注册中心
# ---------------------------------------------------------------------------


def test_task_tree_register_to_authority():
    auth = make_authority()
    tt = TaskTree()
    tt.register_to(auth)
    # 所有实体均已注册
    for eid in tt.entities:
        assert auth.is_registered(eid), f"entity {eid!r} not registered"
    # device_id 正确
    assert auth.registered[tt.task_tree_scope_id].device_id == tt.device_id


def test_delegate_capability_can_be_granted():
    """delegate capability 注册到 Authority 后可授予并求值。"""
    auth = make_authority()
    tt = TaskTree()
    tt.register_to(auth)
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", tt.delegate_capability)
    decision = auth.authorize("agent.1", tt.delegate_capability)
    assert decision.effect is GrantEffect.ALLOWED


# ---------------------------------------------------------------------------
# 7. Authority.injection_for 可从注册后 task-tree-scope 收集注入内容
# ---------------------------------------------------------------------------


def test_task_tree_injection_flows_through_authority():
    auth = make_authority()
    tt = TaskTree()
    tt.register_to(auth)
    auth.grant_membership("agent.1", "pos-1")
    auth.grant_capability("pos-1", tt.task_tree_scope_id, priority=5)
    injections = auth.injection_for("agent.1")
    assert any(
        inj.entity_id == tt.task_tree_scope_id and "[TASK_INSTRUCTION]" in inj.source_tag
        for inj in injections
    ), "TaskTree InjectionDecl not found in injections"


# ---------------------------------------------------------------------------
# 8. make_handle_delegate 在设备化后注入链完整（TransactionBuffer 注入）
# ---------------------------------------------------------------------------


def test_make_handle_delegate_returns_callable():
    tt = TaskTree()
    handler = tt.make_handle_delegate()
    assert callable(handler)


def test_make_handle_delegate_stages_effects():
    """make_handle_delegate 在 TransactionBuffer 注入后能 stage 两个效果。"""
    from unittest.mock import MagicMock

    from my_team.transaction import TransactionBuffer

    buf = TransactionBuffer()
    tt = TaskTree(transaction_buffer=buf)
    handler = tt.make_handle_delegate()

    ctx = MagicMock()
    ctx.agent_id = "agent.boss"
    ctx.tick = 1

    result = handler(
        ctx,
        recipient_agent_id="agent.worker",
        task_title="Do the thing",
        task_description="Details here",
    )
    assert result.success is True
    assert result.data["staged"] is True
    task_id = result.data["task_id"]
    assert task_id.startswith("task.")
    # TransactionBuffer 中有两个 staged effect（TASK_CREATE + EMAIL_SEND）
    staged = buf.get_effects()
    assert len(staged) == 2
    from my_team.transaction import EffectType

    effect_types = {e.effect_type for e in staged}
    assert EffectType.TASK_CREATE in effect_types
    assert EffectType.EMAIL_SEND in effect_types


def test_make_handle_delegate_without_buffer():
    """无 TransactionBuffer 注入时不报错（返回 staged=True 但实际未 stage）。"""
    from unittest.mock import MagicMock

    tt = TaskTree()  # no buffer
    handler = tt.make_handle_delegate()

    ctx = MagicMock()
    ctx.agent_id = "agent.boss"
    ctx.tick = 1

    result = handler(
        ctx,
        recipient_agent_id="agent.worker",
        task_title="Test task",
        task_description="desc",
    )
    assert result.success is True
    assert result.data["staged"] is True


# ---------------------------------------------------------------------------
# 9. 业务功能不变（TaskTree CRUD/状态机 smoke test）
# ---------------------------------------------------------------------------


def test_task_tree_business_still_works():
    from my_team.models.task import TaskStatus

    tt = TaskTree()
    task = tt.create(
        task_id="task.1",
        title="Test task",
        assigner_agent_id="agent.boss",
        assignee_agent_id="agent.worker",
        description="Do something",
        tick=1,
    )
    assert task.task_id == "task.1"
    assert task.status == TaskStatus.DRAFT
    assert tt.count() == 1


def test_task_tree_cancel_cascade_still_works():
    from my_team.models.task import TaskStatus

    tt = TaskTree()
    tt.create("task.p", "Parent", "boss", "worker", tick=1)
    tt.create("task.c", "Child", "worker", "sub", derived_from="task.p", tick=1)
    cancelled = tt.cancel_task("task.p", tick=2)
    ids = {t.task_id for t in cancelled}
    assert "task.p" in ids
    assert "task.c" in ids
    for t in cancelled:
        assert t.status == TaskStatus.CANCELLED
