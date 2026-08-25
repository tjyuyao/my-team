"""记忆工具集 handler（N4-4 CONSOLIDATING 工具面收窄目标）。

对应 SPEC §4.4 与 N4_MEMORY_INJECTION_DESIGN.md §5：

- CONSOLIDATING 下授权集切换为记忆工具集（memory_fold / memory_promote /
  memory_edit / memory_retag / memory_evict / memory_pin）；
- 每个公共函数返回 handler callable（标准签名
  ``(context: ToolContext, **kwargs) -> ToolResult``），注册方式与
  agent_tools.py 的 make_handle_* 模式一致（simulation.register_tool）；
- 全部动作 = Journal effect（MEMORY_ENTRY_WRITE/EVICT/FOLD/MEMORY_PIN，
  可审计可回滚，INVERT_CONTRACT 已注册）；
- 外加载条目（injected）不入 store ⇒ 结构性不可改写（store.get 返回
  None，handler 直接拒绝）。

handler 通过工厂闭包注入各 agent 的记忆子系统（agent_memories /
recall_engines / recall_configs 字典，按 context.agent_id 取用）——
与 context_compiler 的注入方式一致，handler 不接触 Simulation 内部。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from my_team.agent_runtime import ToolContext, ToolResult
from my_team.models.memory import (
    EntryProvenance,
    MemoryEntry,
    MemoryEntryType,
    PersonContent,
    SkillContent,
    TaskContent,
    TaskResultRef,
    ToolContent,
    make_skill_entry,
)

if TYPE_CHECKING:
    from my_team.memory_recall import RecallConfig, RecallEngine
    from my_team.memory_store import AgentMemory
    from my_team.transaction import TransactionBuffer

# 类型 → content 构造（memory_fold/memory_edit 保留原条目类型）
_ContentBuilder = Callable[[str], Any]


def _build_content(entry_type: MemoryEntryType, content_text: str) -> Any:
    """按条目类型构造同型 content（文本折叠/编辑入口）。"""
    if entry_type == MemoryEntryType.TASK:
        return TaskContent(notes=content_text)
    if entry_type == MemoryEntryType.SKILL:
        return SkillContent(sop_text=content_text)
    if entry_type == MemoryEntryType.TOOL:
        return ToolContent(source=content_text, entry="")
    if entry_type == MemoryEntryType.PERSON:
        return PersonContent(profile=content_text)
    raise ValueError(f"未知条目类型: {entry_type}")


def _lookup(
    context: ToolContext,
    agent_memories: dict[str, "AgentMemory"],
    recall_engines: dict[str, "RecallEngine"],
) -> tuple["AgentMemory | None", "RecallEngine | None"]:
    """按 context.agent_id 取该 agent 的记忆存储与召回引擎。"""
    return agent_memories.get(context.agent_id), recall_engines.get(context.agent_id)


def _missing_subsystem(tool_name: str, context: ToolContext, what: str) -> ToolResult:
    return ToolResult(
        success=False,
        error=f"{tool_name}: agent {context.agent_id} 无 {what}（未接线）",
        agent_id=context.agent_id,
        tool_name=tool_name,
        tick=context.tick,
    )


# ---------------------------------------------------------------------------
# memory_fold —— 折叠版本链/注入片段为浓缩条目（MEMORY_ENTRY_FOLD）
# ---------------------------------------------------------------------------


def make_handle_memory_fold(
    agent_memories: dict[str, "AgentMemory"],
    recall_engines: dict[str, "RecallEngine"],
    transaction_buffer: "TransactionBuffer",
) -> Callable[..., Any]:
    """Return the ``memory_fold`` tool handler.

    以浓缩文本替换现有版本链（保留 entry_id，版本号递增）。折叠后
    条目类型与原条目一致（版本链不可变，变更 = 新版本追加）。
    """

    def handle_memory_fold(
        context: ToolContext,
        entry_id: str = "",
        content_text: str = "",
        title: str = "",
        memory_points: list[str] | None = None,
        **_kw: Any,
    ) -> Any:
        if not entry_id or not content_text:
            return ToolResult(
                success=False,
                error="memory_fold 需要 entry_id 与 content_text",
                error_code="INVALID_ARGUMENT",
                agent_id=context.agent_id,
                tool_name="memory_fold",
                tick=context.tick,
            )
        store, engine = _lookup(context, agent_memories, recall_engines)
        if store is None or engine is None:
            return _missing_subsystem("memory_fold", context, "AgentMemory/RecallEngine")
        entry = store.get(entry_id)
        if entry is None:
            return ToolResult(
                success=False,
                error=f"memory_fold: 条目 {entry_id} 不在 store（外加载条目不可改写）",
                agent_id=context.agent_id,
                tool_name="memory_fold",
                tick=context.tick,
            )
        folded = MemoryEntry(
            entry_id=entry.entry_id,
            type=entry.type,
            title=title or entry.title,
            content=_build_content(entry.type, content_text),
            memory_points=list(memory_points) if memory_points is not None else entry.memory_points,
            version=entry.version + 1,
            provenance=entry.provenance,
        )
        store.fold(entry_id, folded, transaction_buffer)
        engine.sync_fold(folded)
        return ToolResult(
            success=True,
            data={"entry_id": str(entry.entry_id), "version": folded.version, "folded": True},
            agent_id=context.agent_id,
            tool_name="memory_fold",
            tick=context.tick,
        )

    return handle_memory_fold


# ---------------------------------------------------------------------------
# memory_promote —— 提炼为长期 skill 条目（可关联 task_id 作结果 provenance）
# ---------------------------------------------------------------------------


def make_handle_memory_promote(
    agent_memories: dict[str, "AgentMemory"],
    recall_engines: dict[str, "RecallEngine"],
    transaction_buffer: "TransactionBuffer",
) -> Callable[..., Any]:
    """Return the ``memory_promote`` tool handler.

    新建 skill 条目（新 entry_id）；可关联 task_id/outcome 作为
    结果 provenance（TaskResultRef，SPEC §4.2「犯错中改进」数据基础）。
    """

    def handle_memory_promote(
        context: ToolContext,
        entry_id: str = "",
        title: str = "",
        sop_text: str = "",
        applies_to: list[str] | None = None,
        memory_points: list[str] | None = None,
        task_id: str = "",
        outcome: str = "",
        note: str = "",
        **_kw: Any,
    ) -> Any:
        store, engine = _lookup(context, agent_memories, recall_engines)
        if store is None or engine is None:
            return _missing_subsystem("memory_promote", context, "AgentMemory/RecallEngine")
        source = store.get(entry_id) if entry_id else None
        if entry_id and source is None:
            return ToolResult(
                success=False,
                error=f"memory_promote: 源条目 {entry_id} 不在 store",
                agent_id=context.agent_id,
                tool_name="memory_promote",
                tick=context.tick,
            )
        if not sop_text:
            if source is None:
                return ToolResult(
                    success=False,
                    error="memory_promote 需要 sop_text 或有效 entry_id",
                    error_code="INVALID_ARGUMENT",
                    agent_id=context.agent_id,
                    tool_name="memory_promote",
                    tick=context.tick,
                )
            content_lines = [
                f"{k}: {v}" for k, v in source.content.model_dump().items() if v
            ]
            sop_text = "\n".join([source.title, *content_lines])
        provenance = EntryProvenance(
            task_results=(
                [TaskResultRef(task_id=task_id, outcome=outcome, note=note)]
                if task_id
                else []
            )
        )
        entry = make_skill_entry(
            title=title or (source.title if source else "提炼技能"),
            sop_text=sop_text,
            applies_to=applies_to or [],
            memory_points=memory_points or [],
            provenance=provenance,
        )
        store.put(entry, transaction_buffer)
        engine.sync_put(entry)
        return ToolResult(
            success=True,
            data={"entry_id": str(entry.entry_id), "version": entry.version, "promoted": True},
            agent_id=context.agent_id,
            tool_name="memory_promote",
            tick=context.tick,
        )

    return handle_memory_promote


# ---------------------------------------------------------------------------
# memory_edit —— 编辑条目（新版本追加，MEMORY_ENTRY_WRITE）
# ---------------------------------------------------------------------------


def make_handle_memory_edit(
    agent_memories: dict[str, "AgentMemory"],
    recall_engines: dict[str, "RecallEngine"],
    transaction_buffer: "TransactionBuffer",
) -> Callable[..., Any]:
    """Return the ``memory_edit`` tool handler。

    修改条目的新版本（title/content_text/memory_points），版本号递增。
    """

    def handle_memory_edit(
        context: ToolContext,
        entry_id: str = "",
        content_text: str = "",
        title: str = "",
        memory_points: list[str] | None = None,
        **_kw: Any,
    ) -> Any:
        if not entry_id:
            return ToolResult(
                success=False,
                error="memory_edit 需要 entry_id",
                error_code="INVALID_ARGUMENT",
                agent_id=context.agent_id,
                tool_name="memory_edit",
                tick=context.tick,
            )
        store, engine = _lookup(context, agent_memories, recall_engines)
        if store is None or engine is None:
            return _missing_subsystem("memory_edit", context, "AgentMemory/RecallEngine")
        entry = store.get(entry_id)
        if entry is None:
            return ToolResult(
                success=False,
                error=f"memory_edit: 条目 {entry_id} 不在 store（外加载条目不可改写）",
                agent_id=context.agent_id,
                tool_name="memory_edit",
                tick=context.tick,
            )
        new_entry = MemoryEntry(
            entry_id=entry.entry_id,
            type=entry.type,
            title=title or entry.title,
            content=_build_content(entry.type, content_text) if content_text else entry.content,
            memory_points=list(memory_points) if memory_points is not None else entry.memory_points,
            version=entry.version + 1,
            provenance=entry.provenance,
        )
        store.put(new_entry, transaction_buffer)
        engine.sync_put(new_entry)
        return ToolResult(
            success=True,
            data={"entry_id": str(entry.entry_id), "version": new_entry.version, "edited": True},
            agent_id=context.agent_id,
            tool_name="memory_edit",
            tick=context.tick,
        )

    return handle_memory_edit


# ---------------------------------------------------------------------------
# memory_retag —— 维护触发器（memory_points，新版本，MEMORY_ENTRY_WRITE）
# ---------------------------------------------------------------------------


def make_handle_memory_retag(
    agent_memories: dict[str, "AgentMemory"],
    recall_engines: dict[str, "RecallEngine"],
    transaction_buffer: "TransactionBuffer",
) -> Callable[..., Any]:
    """Return the ``memory_retag`` tool handler（改触发器/索引词）。"""

    def handle_memory_retag(
        context: ToolContext,
        entry_id: str = "",
        memory_points: list[str] | None = None,
        **_kw: Any,
    ) -> Any:
        if not entry_id or not memory_points:
            return ToolResult(
                success=False,
                error="memory_retag 需要 entry_id 与 memory_points",
                error_code="INVALID_ARGUMENT",
                agent_id=context.agent_id,
                tool_name="memory_retag",
                tick=context.tick,
            )
        store, engine = _lookup(context, agent_memories, recall_engines)
        if store is None or engine is None:
            return _missing_subsystem("memory_retag", context, "AgentMemory/RecallEngine")
        entry = store.get(entry_id)
        if entry is None:
            return ToolResult(
                success=False,
                error=f"memory_retag: 条目 {entry_id} 不在 store（外加载条目不可改写）",
                agent_id=context.agent_id,
                tool_name="memory_retag",
                tick=context.tick,
            )
        new_entry = MemoryEntry(
            entry_id=entry.entry_id,
            type=entry.type,
            title=entry.title,
            content=entry.content,
            memory_points=list(memory_points),
            version=entry.version + 1,
            provenance=entry.provenance,
        )
        store.put(new_entry, transaction_buffer)
        engine.sync_put(new_entry)
        return ToolResult(
            success=True,
            data={"entry_id": str(entry.entry_id), "version": new_entry.version, "retagged": True},
            agent_id=context.agent_id,
            tool_name="memory_retag",
            tick=context.tick,
        )

    return handle_memory_retag


# ---------------------------------------------------------------------------
# memory_evict —— 撤出（移出工作集，MEMORY_ENTRY_EVICT）
# ---------------------------------------------------------------------------


def make_handle_memory_evict(
    agent_memories: dict[str, "AgentMemory"],
    recall_engines: dict[str, "RecallEngine"],
    transaction_buffer: "TransactionBuffer",
) -> Callable[..., Any]:
    """Return the ``memory_evict`` tool handler（从 store 中移除条目）。"""

    def handle_memory_evict(
        context: ToolContext,
        entry_id: str = "",
        **_kw: Any,
    ) -> Any:
        if not entry_id:
            return ToolResult(
                success=False,
                error="memory_evict 需要 entry_id",
                error_code="INVALID_ARGUMENT",
                agent_id=context.agent_id,
                tool_name="memory_evict",
                tick=context.tick,
            )
        store, engine = _lookup(context, agent_memories, recall_engines)
        if store is None or engine is None:
            return _missing_subsystem("memory_evict", context, "AgentMemory/RecallEngine")
        effect = store.evict(entry_id, transaction_buffer)
        if effect is None:
            return ToolResult(
                success=False,
                error=f"memory_evict: 条目 {entry_id} 不在 store",
                agent_id=context.agent_id,
                tool_name="memory_evict",
                tick=context.tick,
            )
        engine.sync_evict(entry_id)
        return ToolResult(
            success=True,
            data={"entry_id": str(entry_id), "evicted": True},
            agent_id=context.agent_id,
            tool_name="memory_evict",
            tick=context.tick,
        )

    return handle_memory_evict


# ---------------------------------------------------------------------------
# memory_pin —— 固定（并入可控查询词，防召回降级，MEMORY_PIN）
# ---------------------------------------------------------------------------


def make_handle_memory_pin(
    agent_memories: dict[str, "AgentMemory"],
    recall_engines: dict[str, "RecallEngine"],
    recall_configs: dict[str, "RecallConfig"],
    transaction_buffer: "TransactionBuffer",
) -> Callable[..., Any]:
    """Return the ``memory_pin`` tool handler。

    把条目的 title + memory_points 并入可控查询词（persistent_query_terms，
    去重）——触发词常驻查询空间，防召回降级（SPEC §4.4）。
    """

    def handle_memory_pin(
        context: ToolContext,
        entry_id: str = "",
        **_kw: Any,
    ) -> Any:
        if not entry_id:
            return ToolResult(
                success=False,
                error="memory_pin 需要 entry_id",
                error_code="INVALID_ARGUMENT",
                agent_id=context.agent_id,
                tool_name="memory_pin",
                tick=context.tick,
            )
        store, engine = _lookup(context, agent_memories, recall_engines)
        if store is None or engine is None:
            return _missing_subsystem("memory_pin", context, "AgentMemory/RecallEngine")
        config = recall_configs.get(context.agent_id)
        if config is None:
            return _missing_subsystem("memory_pin", context, "RecallConfig")
        entry = store.get(entry_id)
        if entry is None:
            return ToolResult(
                success=False,
                error=f"memory_pin: 条目 {entry_id} 不在 store（外加载条目不可改写）",
                agent_id=context.agent_id,
                tool_name="memory_pin",
                tick=context.tick,
            )
        effect = engine.apply_pin_effect(config, entry, transaction_buffer, context.agent_id)
        return ToolResult(
            success=True,
            data={
                "entry_id": str(entry.entry_id),
                "pinned": True,
                "added_terms": effect.data.get("added_terms", []),
            },
            agent_id=context.agent_id,
            tool_name="memory_pin",
            tick=context.tick,
        )

    return handle_memory_pin
