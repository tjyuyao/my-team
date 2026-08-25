"""N4-1 记忆模型测试（schema 校验、版本链、Provenance）。

覆盖要点（N4_MEMORY_INJECTION_DESIGN §8/§9 验收）：
- type-content 一致性（构造即校验）；
- content 禁 UUID（id 走 associated）；
- version >= 1；
- EntryProvenance：injected 必须有 injection_ref；
- 结果 provenance（TaskResultRef，v2 新增）；
- MemoryEntry frozen（不可原地改写）；
- 便利构造函数正常工作。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from my_team.models.memory import (
    EntryOrigin,
    EntryProvenance,
    InjectionRef,
    MemoryEntry,
    MemoryEntryType,
    PersonContent,
    SkillContent,
    TaskContent,
    TaskResultRef,
    ToolContent,
    make_person_entry,
    make_skill_entry,
    make_task_entry,
    make_tool_entry,
)

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _valid_task_entry() -> MemoryEntry:
    return make_task_entry(title="任务1", notes="处理退款", progress="50%")


def _valid_skill_entry() -> MemoryEntry:
    return make_skill_entry(title="退款 SOP", sop_text="步骤一：核实金额；步骤二：审批")


def _valid_tool_entry() -> MemoryEntry:
    return make_tool_entry(
        title="计算工具",
        source="def calc(x): return x * 2",
        entry="calc",
    )


def _valid_person_entry() -> MemoryEntry:
    return make_person_entry(title="客户张三", profile="高价值客户")


# ---------------------------------------------------------------------------
# 基础构造测试
# ---------------------------------------------------------------------------


class TestBasicConstruction:
    """基础构造与字段默认值。"""

    def test_task_entry_created_successfully(self) -> None:
        """task 类型条目正常构造。"""
        e = _valid_task_entry()
        assert e.type == MemoryEntryType.TASK
        assert isinstance(e.content, TaskContent)
        assert e.version == 1
        assert isinstance(e.entry_id, uuid.UUID)

    def test_skill_entry_created_successfully(self) -> None:
        """skill 类型条目正常构造。"""
        e = _valid_skill_entry()
        assert e.type == MemoryEntryType.SKILL
        assert isinstance(e.content, SkillContent)

    def test_tool_entry_created_successfully(self) -> None:
        """tool 类型条目正常构造。"""
        e = _valid_tool_entry()
        assert e.type == MemoryEntryType.TOOL
        assert isinstance(e.content, ToolContent)

    def test_person_entry_created_successfully(self) -> None:
        """person 类型条目正常构造。"""
        e = _valid_person_entry()
        assert e.type == MemoryEntryType.PERSON
        assert isinstance(e.content, PersonContent)

    def test_entry_id_auto_generated_as_uuid4(self) -> None:
        """entry_id 自动生成为 UUID v4。"""
        e = _valid_task_entry()
        assert e.entry_id.version == 4

    def test_two_entries_have_different_ids(self) -> None:
        """两次构造得到不同 entry_id。"""
        e1 = _valid_task_entry()
        e2 = _valid_task_entry()
        assert e1.entry_id != e2.entry_id

    def test_custom_entry_id_respected(self) -> None:
        """指定 entry_id 时使用传入值。"""
        eid = uuid.uuid4()
        e = make_task_entry(title="t", entry_id=eid)
        assert e.entry_id == eid


# ---------------------------------------------------------------------------
# type-content 一致性校验
# ---------------------------------------------------------------------------


class TestTypeContentConsistency:
    """type 与 content 必须匹配（构造即校验）。"""

    def test_task_type_with_skill_content_raises(self) -> None:
        """type=task + SkillContent 构造时抛 ValidationError。"""
        with pytest.raises(ValidationError, match="content"):
            MemoryEntry(
                type=MemoryEntryType.TASK,
                title="wrong",
                content=SkillContent(sop_text="sop"),
            )

    def test_skill_type_with_task_content_raises(self) -> None:
        """type=skill + TaskContent 构造时抛 ValidationError。"""
        with pytest.raises(ValidationError, match="content"):
            MemoryEntry(
                type=MemoryEntryType.SKILL,
                title="wrong",
                content=TaskContent(notes="notes"),
            )

    def test_tool_type_with_person_content_raises(self) -> None:
        """type=tool + PersonContent 构造时抛 ValidationError。"""
        with pytest.raises(ValidationError, match="content"):
            MemoryEntry(
                type=MemoryEntryType.TOOL,
                title="wrong",
                content=PersonContent(profile="p"),
            )

    def test_person_type_with_tool_content_raises(self) -> None:
        """type=person + ToolContent 构造时抛 ValidationError。"""
        with pytest.raises(ValidationError, match="content"):
            MemoryEntry(
                type=MemoryEntryType.PERSON,
                title="wrong",
                content=ToolContent(source="s", entry="e"),
            )


# ---------------------------------------------------------------------------
# content 禁 UUID（schema 校验+测试）
# ---------------------------------------------------------------------------


UUID4_SAMPLE = "550e8400-e29b-41d4-a716-446655440000"


class TestContentNoUUID:
    """content 字段禁止嵌入 UUID（id 须走 associated）。"""

    def test_task_notes_with_uuid_raises(self) -> None:
        """TaskContent.notes 含 UUID v4 → ValidationError。"""
        with pytest.raises(ValidationError):
            TaskContent(notes=f"相关任务是 {UUID4_SAMPLE}")

    def test_task_progress_with_uuid_raises(self) -> None:
        """TaskContent.progress 含 UUID → ValidationError。"""
        with pytest.raises(ValidationError):
            TaskContent(progress=f"uuid: {UUID4_SAMPLE}")

    def test_skill_sop_text_with_uuid_raises(self) -> None:
        """SkillContent.sop_text 含 UUID → ValidationError。"""
        with pytest.raises(ValidationError):
            SkillContent(sop_text=f"关联设备 {UUID4_SAMPLE}")

    def test_skill_applies_to_with_uuid_raises(self) -> None:
        """SkillContent.applies_to 列表项含 UUID → ValidationError。"""
        with pytest.raises(ValidationError):
            SkillContent(sop_text="sop", applies_to=[UUID4_SAMPLE])

    def test_tool_source_with_uuid_raises(self) -> None:
        """ToolContent.source 含 UUID → ValidationError。"""
        with pytest.raises(ValidationError):
            ToolContent(source=f"device_id = '{UUID4_SAMPLE}'", entry="f")

    def test_person_profile_with_uuid_raises(self) -> None:
        """PersonContent.profile 含 UUID → ValidationError。"""
        with pytest.raises(ValidationError):
            PersonContent(profile=f"id={UUID4_SAMPLE}")

    def test_uuid_in_associated_is_valid(self) -> None:
        """id 走 associated 是合法用法——不抛异常。"""
        assoc_id = uuid.uuid4()
        e = make_task_entry(
            title="带关联的任务",
            notes="处理退款",
            associated=[assoc_id],
        )
        assert assoc_id in e.associated

    def test_non_uuid_string_in_content_is_valid(self) -> None:
        """普通字符串不含 UUID → 正常构造。"""
        e = make_task_entry(title="t", notes="这是普通文本，无 UUID")
        assert e.content.notes == "这是普通文本，无 UUID"

    def test_partial_uuid_in_content_is_valid(self) -> None:
        """content 中类 UUID 但不完整的字符串不触发校验。"""
        # 不是完整 UUID v4 格式
        e = make_task_entry(title="t", notes="部分 550e8400-e29b")
        assert "550e8400" in e.content.notes


# ---------------------------------------------------------------------------
# version 约束
# ---------------------------------------------------------------------------


class TestVersionConstraints:
    """version >= 1，不接受 0 或负数。"""

    def test_version_one_is_default(self) -> None:
        """默认 version=1。"""
        e = _valid_task_entry()
        assert e.version == 1

    def test_version_zero_raises(self) -> None:
        """version=0 → ValidationError。"""
        with pytest.raises(ValidationError):
            make_task_entry(title="t", version=0)

    def test_version_negative_raises(self) -> None:
        """version=-1 → ValidationError。"""
        with pytest.raises(ValidationError):
            make_task_entry(title="t", version=-1)

    def test_explicit_higher_version_accepted(self) -> None:
        """显式传入高版本号可接受。"""
        e = make_task_entry(title="t", version=5)
        assert e.version == 5


# ---------------------------------------------------------------------------
# frozen（不可变）
# ---------------------------------------------------------------------------


class TestMemoryEntryFrozen:
    """MemoryEntry frozen=True，不可原地改写（版本链语义）。"""

    def test_cannot_mutate_title(self) -> None:
        """尝试原地修改 title → 抛 ValidationError 或 TypeError。"""
        e = _valid_task_entry()
        with pytest.raises((ValidationError, TypeError)):
            e.title = "新标题"  # type: ignore[misc]

    def test_cannot_mutate_version(self) -> None:
        """尝试原地修改 version → 抛异常。"""
        e = _valid_task_entry()
        with pytest.raises((ValidationError, TypeError)):
            e.version = 2  # type: ignore[misc]

    def test_new_version_via_copy_with(self) -> None:
        """版本链语义：用 model_copy 创建新版本实例，原实例不变。"""
        e_v1 = _valid_task_entry()
        e_v2 = e_v1.model_copy(update={"version": 2, "content": TaskContent(notes="更新内容")})
        assert e_v1.version == 1
        assert e_v2.version == 2
        assert e_v2.entry_id == e_v1.entry_id  # 同一条目


# ---------------------------------------------------------------------------
# EntryProvenance 测试
# ---------------------------------------------------------------------------


class TestEntryProvenance:
    """EntryProvenance：origin/injection_ref 校验。"""

    def test_default_provenance_is_own(self) -> None:
        """默认 provenance.origin = own。"""
        e = _valid_task_entry()
        assert e.provenance.origin == EntryOrigin.OWN

    def test_injected_without_ref_raises(self) -> None:
        """origin=injected 但无 injection_ref → ValidationError。"""
        with pytest.raises(ValidationError, match="injection_ref"):
            EntryProvenance(origin=EntryOrigin.INJECTED, injection_ref=None)

    def test_injected_with_ref_ok(self) -> None:
        """origin=injected + injection_ref 快照 → 正常。"""
        prov = EntryProvenance(
            origin=EntryOrigin.INJECTED,
            injection_ref=InjectionRef(
                position_id="pos.support",
                entity_id="entity.sop",
                snapshot_version=3,
                snapshot_hash="abc123",
            ),
        )
        assert prov.origin == EntryOrigin.INJECTED
        assert prov.injection_ref is not None

    def test_task_result_ref_appended(self) -> None:
        """结果 provenance（v2）：TaskResultRef 可附加到 task_results。"""
        ref = TaskResultRef(
            task_id="task.refund.001",
            outcome="completed",
            note="退款成功，客户满意",
        )
        prov = EntryProvenance(task_results=[ref])
        assert len(prov.task_results) == 1
        assert prov.task_results[0].outcome == "completed"

    def test_skill_entry_with_result_provenance(self) -> None:
        """skill 条目可携带结果 provenance（见设计文档 §1 v2 新增）。"""
        prov = EntryProvenance(
            task_results=[
                TaskResultRef(task_id="task.A", outcome="completed"),
                TaskResultRef(task_id="task.B", outcome="failed"),
            ]
        )
        e = make_skill_entry(
            title="退款 SOP v2",
            sop_text="改进后的步骤",
            provenance=prov,
        )
        assert len(e.provenance.task_results) == 2


# ---------------------------------------------------------------------------
# make_* 便利函数
# ---------------------------------------------------------------------------


class TestConvenienceFunctions:
    """便利构造函数类型安全性验证。"""

    def test_make_task_entry_returns_task_type(self) -> None:
        e = make_task_entry(title="t", notes="n", progress="p")
        assert e.type == MemoryEntryType.TASK
        assert e.content.notes == "n"

    def test_make_skill_entry_returns_skill_type(self) -> None:
        e = make_skill_entry(title="s", sop_text="sop", applies_to=["退款"])
        assert e.type == MemoryEntryType.SKILL
        assert "退款" in e.content.applies_to

    def test_make_tool_entry_returns_tool_type(self) -> None:
        e = make_tool_entry(title="t", source="code", entry="main")
        assert e.type == MemoryEntryType.TOOL
        assert e.content.entry == "main"

    def test_make_person_entry_returns_person_type(self) -> None:
        e = make_person_entry(title="p", profile="档案", relations="是VIP")
        assert e.type == MemoryEntryType.PERSON
        assert e.content.profile == "档案"

    def test_memory_points_default_empty(self) -> None:
        """默认 memory_points 为空列表。"""
        e = make_task_entry(title="t")
        assert e.memory_points == []

    def test_memory_points_accepted(self) -> None:
        """传入 memory_points 保留。"""
        e = make_task_entry(title="t", memory_points=["退款", "VIP"])
        assert "退款" in e.memory_points
