"""记忆条目模型（MemoryEntry schema）。

对应 SPEC §4.2 记忆条目 与 N4_MEMORY_INJECTION_DESIGN.md §1。

核心设计：
- MemoryEntryType：task / skill / tool / person 四类；
- 类型判别内容联合（构造即校验，type 与 content class 必须匹配）；
- associated: list[UUID] 是唯一 id 通道，content 禁 uuid；
- 版本链不可变（version >= 1，变更 = 新版本追加）；
- EntryProvenance 记录来源（own/injected）+ 结果溯源（task_id）。
"""

from __future__ import annotations

import re
import uuid
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

# UUID v4 字符串正则（松检测，用于 content 禁 uuid 校验）
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)


def _contains_uuid(text: str) -> bool:
    """检测字符串中是否含有 UUID v4 片段。"""
    return bool(_UUID_RE.search(text))


# ---------------------------------------------------------------------------
# 记忆条目类型枚举
# ---------------------------------------------------------------------------


class MemoryEntryType(str, Enum):
    """记忆条目的四种类型（SPEC §4.2）。"""

    TASK = "task"
    SKILL = "skill"
    TOOL = "tool"
    PERSON = "person"


# ---------------------------------------------------------------------------
# 类型感知 content 结构体
# ---------------------------------------------------------------------------


class TaskContent(BaseModel):
    """任务相关记忆 content（type=task）。

    任务上下文笔记、进度描述、决策依据。
    id 一律走 MemoryEntry.associated，此处禁止出现 UUID 字符串。
    """

    notes: str = Field(default="", description="任务上下文笔记")
    progress: str = Field(default="", description="任务进度描述")
    decision_rationale: str = Field(default="", description="决策依据与背景")

    @field_validator("notes", "progress", "decision_rationale", mode="after")
    @classmethod
    def _no_uuid(cls, v: str) -> str:
        if _contains_uuid(v):
            raise ValueError("TaskContent 字段不得含 UUID（id 走 associated）")
        return v


class SkillContent(BaseModel):
    """技能/SOP 记忆 content（type=skill）。

    SOP 文本 + 适用场景列表。
    """

    sop_text: str = Field(description="SOP 文本，操作规程")
    applies_to: list[str] = Field(default_factory=list, description="适用场景/条件列表")

    @field_validator("sop_text", mode="after")
    @classmethod
    def _no_uuid_sop(cls, v: str) -> str:
        if _contains_uuid(v):
            raise ValueError("SkillContent.sop_text 不得含 UUID（id 走 associated）")
        return v

    @field_validator("applies_to", mode="after")
    @classmethod
    def _no_uuid_applies(cls, v: list[str]) -> list[str]:
        for item in v:
            if _contains_uuid(item):
                raise ValueError("SkillContent.applies_to 条目不得含 UUID（id 走 associated）")
        return v


class ToolContent(BaseModel):
    """工具/受限 Python 模组记忆 content（type=tool）。

    受限 Python 模组源码 + 入口 + 能力声明。
    """

    source: str = Field(description="受限 Python 模组源码")
    entry: str = Field(description="模组入口函数名/路径")
    capability_decl: str = Field(default="", description="能力声明描述")

    @field_validator("source", "entry", "capability_decl", mode="after")
    @classmethod
    def _no_uuid(cls, v: str) -> str:
        if _contains_uuid(v):
            raise ValueError("ToolContent 字段不得含 UUID（id 走 associated）")
        return v


class PersonContent(BaseModel):
    """人/agent 相关记忆 content（type=person）。

    档案、关系备注、偏好记录。
    """

    profile: str = Field(default="", description="档案/描述")
    relations: str = Field(default="", description="关系备注")
    preferences: str = Field(default="", description="偏好记录")

    @field_validator("profile", "relations", "preferences", mode="after")
    @classmethod
    def _no_uuid(cls, v: str) -> str:
        if _contains_uuid(v):
            raise ValueError("PersonContent 字段不得含 UUID（id 走 associated）")
        return v


# ---------------------------------------------------------------------------
# Provenance（来源溯源）
# ---------------------------------------------------------------------------


class EntryOrigin(str, Enum):
    """记忆条目的来源类别。"""

    OWN = "own"  # agent 自己写的
    INJECTED = "injected"  # 外加载（经授予注入）


class InjectionRef(BaseModel):
    """外加载条目注入快照（来源只读段，结构性保证不可改写）。"""

    position_id: str | None = Field(default=None, description="授予来源 position")
    entity_id: str | None = Field(default=None, description="授予来源 entity")
    # 不存储 content，只记引用（版本/内容 hash 快照）
    snapshot_version: int | None = Field(default=None, description="注入时版本号")
    snapshot_hash: str | None = Field(default=None, description="注入时 content hash")


class TaskResultRef(BaseModel):
    """结果 provenance（v2 新增）：skill 条目关联的任务结果。

    「这条 skill 在哪个任务里产生了什么结果」——供下次召回参考。
    闭环：任务结果 → Assigner 评判 → CONSOLIDATING 反思 → 更新 skill
    （带结果 provenance，SPEC §4.2 / N4_MEMORY_INJECTION_DESIGN §1）。
    """

    task_id: str = Field(description="产生本条目的任务 id")
    outcome: str = Field(
        description="任务结果标签（completed/failed/escalated/customer_rejected 等）"
    )
    note: str = Field(default="", description="补充说明")


class EntryProvenance(BaseModel):
    """记忆条目溯源信息。"""

    origin: EntryOrigin = Field(default=EntryOrigin.OWN, description="条目来源（own/injected）")
    injection_ref: InjectionRef | None = Field(
        default=None,
        description="外加载时的注入快照（origin=injected 时填写）",
    )
    # 结果 provenance（v2 新增）：memory_promote 关联 task_id 列表
    task_results: list[TaskResultRef] = Field(
        default_factory=list,
        description="关联的任务结果列表（skill 条目带结果证据，闭环学习）",
    )

    @model_validator(mode="after")
    def _check_injection_ref(self) -> EntryProvenance:
        """origin=injected 时必须有 injection_ref 快照。"""
        if self.origin == EntryOrigin.INJECTED and self.injection_ref is None:
            raise ValueError("origin=injected 时必须提供 injection_ref 快照")
        return self


# ---------------------------------------------------------------------------
# MemoryEntry 主体
# ---------------------------------------------------------------------------

# 类型到 content 类的映射（type-content 一致性校验用）
_TYPE_TO_CONTENT_CLS: dict[MemoryEntryType, type] = {
    MemoryEntryType.TASK: TaskContent,
    MemoryEntryType.SKILL: SkillContent,
    MemoryEntryType.TOOL: ToolContent,
    MemoryEntryType.PERSON: PersonContent,
}


class MemoryEntry(BaseModel):
    """记忆条目（SPEC §4.2 / N4_MEMORY_INJECTION_DESIGN §1）。

    不变量：
    - entry_id 自动生成（UUID v4）；
    - type 与 content 类型必须匹配（构造即校验）；
    - content 中禁止出现 UUID（id 走 associated）；
    - version >= 1，不可原地改写（变更 = 追加新版本）；
    - frozen=True（Pydantic 不可变，版本链语义）。
    """

    entry_id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        description="条目唯一 ID（UUID v4）",
    )
    type: MemoryEntryType = Field(description="记忆类型")
    title: str = Field(description="简短标题（渲染/展示用）")
    content: TaskContent | SkillContent | ToolContent | PersonContent = Field(
        description="类型感知 content（type 决定具体结构）"
    )
    memory_points: list[str] = Field(
        default_factory=list,
        description="触发器/索引关键词，主动维护",
    )
    associated: list[uuid.UUID] = Field(
        default_factory=list,
        description="关联对象 id 列表（agent/设备/任务/业务 uuid）",
    )
    version: int = Field(
        default=1,
        ge=1,
        description="版本号（>=1），版本链不可变",
    )
    provenance: EntryProvenance = Field(
        default_factory=EntryProvenance,
        description="条目来源溯源",
    )

    model_config = {"frozen": True}  # 不可变，版本链语义

    @model_validator(mode="after")
    def _validate_type_content_match(self) -> MemoryEntry:
        """校验 type 与 content 的类型一致性（构造即校验）。"""
        expected_cls = _TYPE_TO_CONTENT_CLS[self.type]
        if not isinstance(self.content, expected_cls):
            raise ValueError(
                f"type={self.type.value} 要求 content 为 {expected_cls.__name__}，"
                f"实际得到 {type(self.content).__name__}"
            )
        return self


# ---------------------------------------------------------------------------
# 便利构造函数（类型安全）
# ---------------------------------------------------------------------------


def make_task_entry(
    *,
    title: str,
    notes: str = "",
    progress: str = "",
    decision_rationale: str = "",
    memory_points: list[str] | None = None,
    associated: list[uuid.UUID] | None = None,
    version: int = 1,
    provenance: EntryProvenance | None = None,
    entry_id: uuid.UUID | None = None,
) -> MemoryEntry:
    """构造 task 类型记忆条目。"""
    return MemoryEntry(
        entry_id=entry_id or uuid.uuid4(),
        type=MemoryEntryType.TASK,
        title=title,
        content=TaskContent(
            notes=notes,
            progress=progress,
            decision_rationale=decision_rationale,
        ),
        memory_points=memory_points or [],
        associated=associated or [],
        version=version,
        provenance=provenance or EntryProvenance(),
    )


def make_skill_entry(
    *,
    title: str,
    sop_text: str,
    applies_to: list[str] | None = None,
    memory_points: list[str] | None = None,
    associated: list[uuid.UUID] | None = None,
    version: int = 1,
    provenance: EntryProvenance | None = None,
    entry_id: uuid.UUID | None = None,
) -> MemoryEntry:
    """构造 skill 类型记忆条目。"""
    return MemoryEntry(
        entry_id=entry_id or uuid.uuid4(),
        type=MemoryEntryType.SKILL,
        title=title,
        content=SkillContent(
            sop_text=sop_text,
            applies_to=applies_to or [],
        ),
        memory_points=memory_points or [],
        associated=associated or [],
        version=version,
        provenance=provenance or EntryProvenance(),
    )


def make_tool_entry(
    *,
    title: str,
    source: str,
    entry: str,
    capability_decl: str = "",
    memory_points: list[str] | None = None,
    associated: list[uuid.UUID] | None = None,
    version: int = 1,
    provenance: EntryProvenance | None = None,
    entry_id: uuid.UUID | None = None,
) -> MemoryEntry:
    """构造 tool 类型记忆条目。"""
    return MemoryEntry(
        entry_id=entry_id or uuid.uuid4(),
        type=MemoryEntryType.TOOL,
        title=title,
        content=ToolContent(
            source=source,
            entry=entry,
            capability_decl=capability_decl,
        ),
        memory_points=memory_points or [],
        associated=associated or [],
        version=version,
        provenance=provenance or EntryProvenance(),
    )


def make_person_entry(
    *,
    title: str,
    profile: str = "",
    relations: str = "",
    preferences: str = "",
    memory_points: list[str] | None = None,
    associated: list[uuid.UUID] | None = None,
    version: int = 1,
    provenance: EntryProvenance | None = None,
    entry_id: uuid.UUID | None = None,
) -> MemoryEntry:
    """构造 person 类型记忆条目。"""
    return MemoryEntry(
        entry_id=entry_id or uuid.uuid4(),
        type=MemoryEntryType.PERSON,
        title=title,
        content=PersonContent(
            profile=profile,
            relations=relations,
            preferences=preferences,
        ),
        memory_points=memory_points or [],
        associated=associated or [],
        version=version,
        provenance=provenance or EntryProvenance(),
    )
