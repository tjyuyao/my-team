"""召回引擎（N4-2）。

对应 N4_MEMORY_INJECTION_DESIGN.md §3 与 SPEC §4.3。

核心设计：
- RecallConfig：agent 侧召回状态（可控查询词 + 临时覆盖词）；
  persistent_query_terms 持久影响每 tick 的触发召回；
  temp_overrides 是主动回忆（memory_recall intent）的一次性临时词，
  下 tick 消费后自动清空；
- RecallCandidate：召回候选条目（带来源段标签 source_tag，供
  N4-3 注入组装器使用）；
- RecallBackend 协议：可插拔后端接口（触发器关键词先行；
  向量化后端是 P2 backlog，留接口即可，不实现 embedding）；
- KeywordRecallBackend：触发器关键词/子串匹配实现（召回面 =
  memory_points + title，内容不向量化，可审计）；
- RecallIndex：memory_points → entry_id 倒排索引（在 AgentMemory
  之外独立管理，侵入最小；条目写/折叠/撤出时由调用方同步）；
- RecallEngine：三路词合并 → 后端查询 → top-k 排序输出；
  上下文词（contextual_terms）∪ 可控查询词（persistent_query_terms）
  ∪ 临时覆盖词（temp_overrides，消费后清空）。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from my_team.memory_store import AgentMemory
from my_team.models.memory import MemoryEntry, MemoryEntryType
from my_team.transaction import EffectType, StagedEffect, TransactionBuffer

# ---------------------------------------------------------------------------
# RecallConfig（agent 召回状态，属注入状态空间）
# ---------------------------------------------------------------------------


class RecallConfig(BaseModel):
    """agent 侧召回状态配置（N4-2 可控查询词 + 临时覆盖，属注入状态空间）。

    持久字段：
    - persistent_query_terms：agent 可显式控制，持久影响每 tick 的触发召回；
      通过 MEMORY_RECALL_CONFIG effect 写入（可回滚）。

    一次性字段（消费后自动清空）：
    - temp_overrides：主动回忆（memory_recall intent）写入的临时词列表；
      在下 tick 的召回阶段合并后立即清空（一次性）。
    """

    persistent_query_terms: list[str] = Field(
        default_factory=list,
        description="可控查询词，agent 主动管理，持久影响每 tick 召回",
    )
    temp_overrides: list[str] = Field(
        default_factory=list,
        description="临时覆盖词（memory_recall 写入，下 tick 消费后清空）",
    )


# ---------------------------------------------------------------------------
# RecallCandidate（召回候选条目）
# ---------------------------------------------------------------------------


@dataclass
class RecallCandidate:
    """召回候选条目，携带优先级与来源段标签（供 N4-3 注入组装器使用）。

    Attributes:
        entry: 最新版本的 MemoryEntry（从 AgentMemory 取出）；
        matched_terms: 本次命中的查询词集合（用于优先级排序和审计）；
        trigger_score: 触发优先级分值（命中词数，越高越优先）；
        source_tag: 来源段标签（供注入布局标记，如 [SKILL_INSTRUCTION]）；
        entry_id: 快捷访问 entry_id（str 形式）。
    """

    entry: MemoryEntry
    matched_terms: frozenset[str]
    trigger_score: int
    source_tag: str = ""
    entry_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.entry_id = str(self.entry.entry_id)


# ---------------------------------------------------------------------------
# 来源段标签（source_tag）映射
# ---------------------------------------------------------------------------

# MemoryEntryType → 注入来源段标签（与 N4_MEMORY_INJECTION_DESIGN §4 对应）
_TYPE_TO_SOURCE_TAG: dict[MemoryEntryType, str] = {
    MemoryEntryType.SKILL: "[SKILL_INSTRUCTION]",
    MemoryEntryType.TOOL: "[TOOL_DEFINITION]",
    MemoryEntryType.TASK: "[TASK_CONTEXT]",
    MemoryEntryType.PERSON: "[PERSON_PROFILE]",
}


def _source_tag_for(entry: MemoryEntry) -> str:
    """根据条目类型返回来源段标签。"""
    return _TYPE_TO_SOURCE_TAG.get(entry.type, "[MEMORY]")


# ---------------------------------------------------------------------------
# RecallBackend 协议（可插拔接口）
# ---------------------------------------------------------------------------


@runtime_checkable
class RecallBackend(Protocol):
    """召回后端协议（可插拔）。

    输入：查询词列表 + AgentMemory + RecallIndex；
    输出：候选条目列表（带匹配信息，由 RecallEngine 负责排序）。

    当前内置实现：KeywordRecallBackend（触发器关键词/子串匹配）。
    P2 backlog：向量化后端（向量化对象 = 触发器文本，内容不向量化）。
    """

    def query(
        self,
        terms: list[str],
        store: AgentMemory,
        index: RecallIndex,
        *,
        filter_types: list[MemoryEntryType] | None = None,
    ) -> list[RecallCandidate]:
        """执行一次召回查询。

        Args:
            terms: 查询词列表（三路词合并后的结果）；
            store: agent 记忆存储（AgentMemory，最新版本取用）；
            index: 触发器倒排索引（与 store 保持同步）；
            filter_types: 限制召回的 MemoryEntryType（None = 不限制）。

        Returns:
            候选条目列表（未排序，由 RecallEngine 排序）。
        """
        ...


# ---------------------------------------------------------------------------
# RecallIndex（触发器倒排索引）
# ---------------------------------------------------------------------------


class RecallIndex:
    """memory_points 触发器倒排索引（独立模块，侵入 AgentMemory 最小）。

    索引结构：trigger_token（小写归一化）→ set[entry_id_str]。
    索引同步：条目写/折叠/撤出时，由调用方（RecallEngine 或外部）
    调用 on_put / on_evict / on_fold 更新。

    召回面 = memory_points + title（title 作为隐式触发器）；
    内容（content.*）**不入索引**，可审计。

    注意：此索引不跟踪 AgentMemory 内部的版本链变化——条目的
    触发器在版本更迭时，需由写入方在 on_put / on_fold 时更新。
    """

    def __init__(self) -> None:
        # trigger_token → set[entry_id_str]
        self._index: dict[str, set[str]] = defaultdict(set)
        # entry_id_str → 当前版本触发器集合（用于差量更新）
        self._entry_triggers: dict[str, frozenset[str]] = {}

    # ------------------------------------------------------------------
    # 同步接口（与 AgentMemory 写操作配套）
    # ------------------------------------------------------------------

    def on_put(self, entry: MemoryEntry) -> None:
        """条目写入/追加版本时同步索引（差量更新触发器）。

        更新语义：用新版本的触发器集合替换旧版本（title + memory_points）。
        """
        eid = str(entry.entry_id)
        new_triggers = self._extract_triggers(entry)

        old_triggers = self._entry_triggers.get(eid, frozenset())
        # 移除旧触发器
        for tok in old_triggers - new_triggers:
            self._index[tok].discard(eid)
            if not self._index[tok]:
                del self._index[tok]
        # 添加新触发器
        for tok in new_triggers - old_triggers:
            self._index[tok].add(eid)

        self._entry_triggers[eid] = new_triggers

    def on_evict(self, entry_id: uuid.UUID | str) -> None:
        """条目撤出时从索引中移除。"""
        eid = str(entry_id)
        old_triggers = self._entry_triggers.pop(eid, frozenset())
        for tok in old_triggers:
            self._index[tok].discard(eid)
            if not self._index[tok]:
                del self._index[tok]

    def on_fold(self, folded_entry: MemoryEntry) -> None:
        """条目折叠时重建触发器（以折叠后新版本为准）。"""
        self.on_put(folded_entry)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def lookup(self, token: str) -> frozenset[str]:
        """精确 token 查询，返回命中的 entry_id 集合。"""
        return frozenset(self._index.get(token.lower(), set()))

    def substring_search(self, query: str) -> frozenset[str]:
        """子串匹配：返回任意触发器 token 包含 query（小写）的 entry_id 集合。

        比精确 token 查询慢，但支持模糊触发词（如 "退款" 命中 "处理退款申请"）。
        """
        q = query.lower()
        result: set[str] = set()
        for tok, eids in self._index.items():
            if q in tok:
                result |= eids
        return frozenset(result)

    def audit_triggers(self) -> dict[str, list[str]]:
        """导出完整触发器 → entry_id 映射（审计用，召回面可视）。"""
        return {tok: sorted(eids) for tok, eids in self._index.items()}

    def entry_triggers(self, entry_id: uuid.UUID | str) -> frozenset[str]:
        """返回指定条目当前版本的触发器集合（审计用）。"""
        return self._entry_triggers.get(str(entry_id), frozenset())

    def __len__(self) -> int:
        """索引中 token 数量。"""
        return len(self._index)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_triggers(entry: MemoryEntry) -> frozenset[str]:
        """从条目提取触发器 token 集合（title + memory_points，小写归一化）。"""
        tokens: set[str] = set()
        # title 作为隐式触发器
        if entry.title:
            tokens.add(entry.title.lower())
        # memory_points 显式触发器
        for mp in entry.memory_points:
            if mp:
                tokens.add(mp.lower())
        return frozenset(tokens)


# ---------------------------------------------------------------------------
# KeywordRecallBackend（触发器关键词/子串匹配实现）
# ---------------------------------------------------------------------------


class KeywordRecallBackend:
    """触发器关键词/子串匹配召回后端（RecallBackend 协议实现）。

    召回面 = memory_points + title（触发器列表，可审计）；
    内容（content.*）**不向量化、不入索引**（SPEC 明确）。

    匹配策略：
    1. 精确 token 匹配（query_term == trigger_token）；
    2. 子串匹配（query_term in trigger_token 或 trigger_token in query_term）。

    结果：命中任意查询词的条目均进入候选集，trigger_score = 命中词数。
    """

    def query(
        self,
        terms: list[str],
        store: AgentMemory,
        index: RecallIndex,
        *,
        filter_types: list[MemoryEntryType] | None = None,
    ) -> list[RecallCandidate]:
        """执行关键词/子串匹配召回。

        每个查询词分别走精确查找 + 子串搜索，合并去重；
        统计每个 entry 的命中词数作为 trigger_score。
        """
        if not terms:
            return []

        # entry_id_str → 命中的查询词集合
        hits: dict[str, set[str]] = defaultdict(set)

        for term in terms:
            if not term:
                continue
            term_lower = term.lower()
            # 精确 token 查找
            for eid in index.lookup(term_lower):
                hits[eid].add(term)
            # 子串匹配（含反向子串：token 包含 term，或 term 包含 token）
            for eid in index.substring_search(term_lower):
                hits[eid].add(term)

        if not hits:
            return []

        candidates: list[RecallCandidate] = []
        for eid, matched in hits.items():
            entry = store.get(uuid.UUID(eid))
            if entry is None:
                # 索引与 store 可能短暂不一致（条目被撤出但索引未来得及同步）
                continue
            # 类型过滤
            if filter_types and entry.type not in filter_types:
                continue
            candidates.append(
                RecallCandidate(
                    entry=entry,
                    matched_terms=frozenset(matched),
                    trigger_score=len(matched),
                    source_tag=_source_tag_for(entry),
                )
            )

        return candidates


# ---------------------------------------------------------------------------
# RecallEngine（三路词合并 → 后端查询 → top-k 排序）
# ---------------------------------------------------------------------------


class RecallEngine:
    """召回引擎（N4-2 核心）。

    封装三路词合并、后端查询与 top-k 排序：
    - contextual_terms：上下文词（专注 task/收件箱等，调用方每 tick 传入）；
    - persistent_query_terms：可控查询词（RecallConfig 状态，agent 显式控制）；
    - temp_overrides：临时覆盖词（memory_recall 主动回忆写入，消费后清空）。

    使用示例（agent 侧 tick 内）：
        engine = RecallEngine(backend=KeywordRecallBackend())
        candidates = engine.recall(
            store=agent_memory,
            config=recall_config,
            contextual_terms=["退款", "客户投诉"],
            top_k=5,
        )
        # candidates 按 trigger_score 降序排列，直接送 N4-3 注入组装器
    """

    def __init__(
        self,
        backend: RecallBackend | None = None,
        index: RecallIndex | None = None,
    ) -> None:
        """初始化召回引擎。

        Args:
            backend: 召回后端（默认 KeywordRecallBackend）；
            index: 触发器倒排索引（默认新建，需与 AgentMemory 保持同步）。
        """
        self._backend: RecallBackend = backend or KeywordRecallBackend()
        self._index: RecallIndex = index or RecallIndex()

    @property
    def index(self) -> RecallIndex:
        """触发器倒排索引（供外部调用方在条目变更时同步）。"""
        return self._index

    # ------------------------------------------------------------------
    # 索引同步便捷方法（包装 RecallIndex 对应方法）
    # ------------------------------------------------------------------

    def sync_put(self, entry: MemoryEntry) -> None:
        """条目写入/追加版本后同步触发器索引。"""
        self._index.on_put(entry)

    def sync_evict(self, entry_id: uuid.UUID | str) -> None:
        """条目撤出后从索引中移除。"""
        self._index.on_evict(entry_id)

    def sync_fold(self, folded_entry: MemoryEntry) -> None:
        """条目折叠后重建触发器索引。"""
        self._index.on_fold(folded_entry)

    # ------------------------------------------------------------------
    # 主召回接口
    # ------------------------------------------------------------------

    def recall(
        self,
        store: AgentMemory,
        config: RecallConfig,
        contextual_terms: list[str] | None = None,
        *,
        top_k: int = 10,
        filter_types: list[MemoryEntryType] | None = None,
        consume_temp_overrides: bool = True,
    ) -> list[RecallCandidate]:
        """执行一次完整召回（三路词合并 → 后端查询 → top-k 排序）。

        Args:
            store: agent 记忆存储；
            config: 召回配置（可控查询词 + 临时覆盖词）；
            contextual_terms: 本 tick 上下文词（调用方传入，不修改 config）；
            top_k: 最多返回候选数；
            filter_types: 限制召回类型（None = 不限制）；
            consume_temp_overrides: 是否消费（清空）temp_overrides；
              True（默认）= 消费后清空，实现"一次性"语义；
              False = 只读（测试/审计场景）。

        Returns:
            按 trigger_score 降序排列的 RecallCandidate 列表（最多 top_k 条）。
        """
        # 三路词合并（去重，保留顺序优先级：上下文 > 持久 > 临时）
        merged: list[str] = []
        seen: set[str] = set()

        def _add_terms(terms: list[str]) -> None:
            for t in terms:
                t_lower = t.lower()
                if t_lower not in seen:
                    seen.add(t_lower)
                    merged.append(t)

        _add_terms(contextual_terms or [])
        _add_terms(config.persistent_query_terms)
        _add_terms(config.temp_overrides)

        # 消费 temp_overrides（一次性）
        if consume_temp_overrides and config.temp_overrides:
            config.temp_overrides = []

        if not merged:
            return []

        # 后端查询
        candidates = self._backend.query(
            terms=merged,
            store=store,
            index=self._index,
            filter_types=filter_types,
        )

        # 按 trigger_score 降序排序，取 top_k
        candidates.sort(key=lambda c: c.trigger_score, reverse=True)
        return candidates[:top_k]

    # ------------------------------------------------------------------
    # Effect 接入（与 TransactionBuffer 配合，记录 invert_data）
    # ------------------------------------------------------------------

    def apply_recall_config_effect(
        self,
        config: RecallConfig,
        new_persistent_terms: list[str],
        buffer: TransactionBuffer,
        agent_id: str,
    ) -> StagedEffect:
        """更新可控查询词，产生 MEMORY_RECALL_CONFIG effect（可回滚）。

        invert_data 记录更新前的持久查询词列表，支持 rollback_recall_config_effect。
        """
        before = list(config.persistent_query_terms)
        effect = buffer.stage(
            effect_type=EffectType.MEMORY_RECALL_CONFIG,
            agent_id=agent_id,
            resource=f"recall_config:{agent_id}:persistent_query_terms",
            data={"new_terms": new_persistent_terms},
        )
        effect.invert_data["recall_config_before"] = before
        # 直接应用
        config.persistent_query_terms = list(new_persistent_terms)
        return effect

    def apply_memory_recall_effect(
        self,
        config: RecallConfig,
        temp_terms: list[str],
        buffer: TransactionBuffer,
        agent_id: str,
    ) -> StagedEffect:
        """写入临时召回词，产生 MEMORY_RECALL effect（主动回忆，一次性）。

        invert_data 记录写入的词列表，支持 rollback_memory_recall_effect。
        延迟 1 tick 生效：Act 阶段写入 temp_overrides，Observe 阶段（下 tick）
        消费，结构性延迟非额外等待。
        """
        added = list(temp_terms)
        effect = buffer.stage(
            effect_type=EffectType.MEMORY_RECALL,
            agent_id=agent_id,
            resource=f"recall_config:{agent_id}:temp_overrides",
            data={"temp_terms": added},
        )
        effect.invert_data["temp_overrides_added"] = added
        # 追加到 temp_overrides（不清空已有，允许多次 memory_recall 累积）
        config.temp_overrides = config.temp_overrides + added
        return effect

    # ------------------------------------------------------------------
    # N4-4 整理模式：memory_pin 固定条目（防召回降级）
    # ------------------------------------------------------------------

    def apply_pin_effect(
        self,
        config: RecallConfig,
        entry: MemoryEntry,
        buffer: TransactionBuffer,
        agent_id: str,
    ) -> StagedEffect:
        """固定条目（memory_pin），产生 MEMORY_PIN effect（可回滚）。

        语义（SPEC §4.4）：把条目的 title + memory_points 并入可控
        查询词（persistent_query_terms，去重）——触发词常驻查询空间，
        条目每 tick 必被召回，防召回降级（detail 降级属另一预算机制）。

        invert_data 记录固定前的可控查询词列表，支持 rollback_pin_effect。
        """
        terms = [t for t in ([entry.title] + list(entry.memory_points)) if t]
        before = list(config.persistent_query_terms)
        added = [t for t in terms if t not in before]
        effect = buffer.stage(
            effect_type=EffectType.MEMORY_PIN,
            agent_id=agent_id,
            resource=f"recall_config:{agent_id}:pin:{entry.entry_id}",
            data={
                "entry_id": str(entry.entry_id),
                "added_terms": added,
            },
        )
        effect.invert_data["recall_config_before"] = before
        config.persistent_query_terms = before + added
        return effect

    # ------------------------------------------------------------------
    # 回滚支持
    # ------------------------------------------------------------------

    @staticmethod
    def rollback_recall_config_effect(
        config: RecallConfig,
        effect: StagedEffect,
    ) -> None:
        """回滚 MEMORY_RECALL_CONFIG effect（恢复更新前的持久查询词）。"""
        if effect.effect_type != EffectType.MEMORY_RECALL_CONFIG:
            return
        before = effect.invert_data.get("recall_config_before")
        if before is not None:
            config.persistent_query_terms = list(before)

    @staticmethod
    def rollback_memory_recall_effect(
        config: RecallConfig,
        effect: StagedEffect,
    ) -> None:
        """回滚 MEMORY_RECALL effect（从 temp_overrides 移除写入的词）。"""
        if effect.effect_type != EffectType.MEMORY_RECALL:
            return
        added = effect.invert_data.get("temp_overrides_added", [])
        if added:
            added_set = set(added)
            config.temp_overrides = [t for t in config.temp_overrides if t not in added_set]

    @staticmethod
    def rollback_pin_effect(
        config: RecallConfig,
        effect: StagedEffect,
    ) -> None:
        """回滚 MEMORY_PIN effect（恢复固定前的可控查询词列表）。"""
        if effect.effect_type != EffectType.MEMORY_PIN:
            return
        before = effect.invert_data.get("recall_config_before")
        if before is not None:
            config.persistent_query_terms = list(before)
