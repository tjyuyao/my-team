"""Agent 记忆存储（精炼层）。

归属：Agent 引擎数据面（N4_MEMORY_INJECTION_DESIGN §2）。

设计要点：
- AgentMemory：每 agent 一实例，管理 MemoryEntry 版本链；
- 版本链不可变：变更 = 新版本追加（每个 entry_id 对应一个版本链 list[MemoryEntry]）；
- 外加载条目（injected）投影只读，**不写入 store**（结构性防改写）：
  store 中不存在该 entry_id ⇒ 不可写；
- 记忆写 effect：通过 EffectType.MEMORY_ENTRY_WRITE / EVICT / FOLD
  进入 Journal（invert_data 记旧版本链，支持回滚）。
"""

from __future__ import annotations

import uuid
from typing import Any

from my_team.models.memory import (
    EntryOrigin,
    MemoryEntry,
    MemoryEntryType,
)
from my_team.transaction import (
    EffectType,
    StagedEffect,
    TransactionBuffer,
)


class VersionChain:
    """单条记忆的版本链（append-only list[MemoryEntry]）。

    版本按 version 升序排列，最新版本在末尾。
    内部结构不可直接改写——外部只能通过 append_version() 追加。
    """

    def __init__(self, initial: MemoryEntry) -> None:
        self._chain: list[MemoryEntry] = [initial]

    @property
    def entry_id(self) -> uuid.UUID:
        return self._chain[0].entry_id

    @property
    def current(self) -> MemoryEntry:
        """最新版本。"""
        return self._chain[-1]

    @property
    def history(self) -> list[MemoryEntry]:
        """完整版本链（只读视图）。"""
        return list(self._chain)

    @property
    def version_count(self) -> int:
        return len(self._chain)

    def append_version(self, new_entry: MemoryEntry) -> None:
        """追加新版本（版本号必须严格递增）。"""
        if new_entry.entry_id != self.entry_id:
            raise ValueError(
                f"版本链 entry_id 不匹配：期待 {self.entry_id}，实际 {new_entry.entry_id}"
            )
        expected_version = self._chain[-1].version + 1
        if new_entry.version != expected_version:
            raise ValueError(
                f"版本号必须严格递增：当前最新 {self._chain[-1].version}，"
                f"新版本须为 {expected_version}，实际 {new_entry.version}"
            )
        self._chain.append(new_entry)

    def pop_latest_version(self) -> MemoryEntry:
        """移除并返回最新版本（用于回滚）。不可弹出唯一版本。"""
        if len(self._chain) <= 1:
            raise ValueError("版本链只剩一个版本，无法 pop（应整体移除条目）")
        return self._chain.pop()

    def snapshot(self) -> list[MemoryEntry]:
        """当前版本链快照（用于 invert_data）。"""
        return list(self._chain)


class AgentMemory:
    """Agent 精炼层记忆存储。

    - 每 agent 一实例；
    - 条目以版本链存储（entry_id → VersionChain）；
    - **外加载条目不入库**（projection-only）：
      store 不存在 entry_id ⇒ 任何写操作均拒绝该条目；
      调用方只需检查 entry_id not in store 即可确认"来源段不可改写"；
    - 所有写操作通过 staged_effect 接口（与 TransactionBuffer 配合），
      记录 invert_data 以支持回滚。
    """

    def __init__(self, agent_id: str) -> None:
        self._agent_id = agent_id
        # entry_id (str) → VersionChain
        self._store: dict[str, VersionChain] = {}

    @property
    def agent_id(self) -> str:
        return self._agent_id

    # ------------------------------------------------------------------
    # 只读查询接口
    # ------------------------------------------------------------------

    def get(self, entry_id: uuid.UUID | str) -> MemoryEntry | None:
        """取最新版本，不存在返回 None。"""
        chain = self._store.get(str(entry_id))
        if chain is None:
            return None
        return chain.current

    def get_chain(self, entry_id: uuid.UUID | str) -> list[MemoryEntry]:
        """取完整版本链，不存在返回空列表。"""
        chain = self._store.get(str(entry_id))
        if chain is None:
            return []
        return chain.history

    def list_entries(
        self,
        entry_type: MemoryEntryType | None = None,
    ) -> list[MemoryEntry]:
        """列出所有条目（最新版本），可按 type 过滤。"""
        result = [c.current for c in self._store.values()]
        if entry_type is not None:
            result = [e for e in result if e.type == entry_type]
        return result

    def contains(self, entry_id: uuid.UUID | str) -> bool:
        """检查 entry_id 是否在 store 中（外加载条目不在 store）。"""
        return str(entry_id) in self._store

    # ------------------------------------------------------------------
    # 带 effect 的写操作（需 TransactionBuffer，记录 invert_data）
    # ------------------------------------------------------------------

    def put(
        self,
        entry: MemoryEntry,
        buffer: TransactionBuffer,
    ) -> StagedEffect:
        """新增或追加版本，产生 MEMORY_ENTRY_WRITE effect。

        若 entry_id 已存在，则追加新版本（版本号必须递增）；
        若不存在，则建立新版本链（version 须为 1）。

        外加载条目（origin=injected）**不允许写入 store**——外加载条目
        的语义是"投影只读"，结构性保证其 entry_id 不存在于 store。
        调用方不应传入 injected 条目。
        """
        if entry.provenance.origin == EntryOrigin.INJECTED:
            raise ValueError(
                f"外加载条目（injected）不得写入 AgentMemory store："
                f"entry_id={entry.entry_id}，"
                "来源段不可改写是结构性保证（store 中不存在 = 不可写）"
            )

        eid = str(entry.entry_id)
        # 记录写前状态（invert_data）
        chain = self._store.get(eid)
        if chain is not None:
            before_snapshot = chain.snapshot()
        else:
            before_snapshot = None  # 新建条目，回滚=移除

        effect = buffer.stage(
            effect_type=EffectType.MEMORY_ENTRY_WRITE,
            agent_id=self._agent_id,
            resource=f"memory:{self._agent_id}:{eid}",
            data={"entry_id": eid, "version": entry.version},
        )
        # 记录 invert_data
        effect.invert_data["memory_before"] = (
            eid,
            [e.model_dump() for e in before_snapshot] if before_snapshot is not None else None,
        )

        # 直接应用（在 buffer.commit() 调用前先存）
        self._apply_write(entry)
        return effect

    def evict(
        self,
        entry_id: uuid.UUID | str,
        buffer: TransactionBuffer,
    ) -> StagedEffect | None:
        """撤出条目（从 store 中移除），产生 MEMORY_ENTRY_EVICT effect。

        若 entry_id 不在 store 中则返回 None（无操作）。
        """
        eid = str(entry_id)
        chain = self._store.get(eid)
        if chain is None:
            return None

        before_snapshot = chain.snapshot()
        effect = buffer.stage(
            effect_type=EffectType.MEMORY_ENTRY_EVICT,
            agent_id=self._agent_id,
            resource=f"memory:{self._agent_id}:{eid}",
            data={"entry_id": eid},
        )
        effect.invert_data["memory_before"] = (
            eid,
            [e.model_dump() for e in before_snapshot],
        )
        del self._store[eid]
        return effect

    def fold(
        self,
        entry_id: uuid.UUID | str,
        folded_entry: MemoryEntry,
        buffer: TransactionBuffer,
    ) -> StagedEffect:
        """折叠（合并/压缩）版本链，产生 MEMORY_ENTRY_FOLD effect。

        以 folded_entry 替换现有版本链（保留 entry_id，版本号递增）。
        典型用途：CONSOLIDATING 整理模式压缩多版本为一个摘要版本。
        """
        eid = str(entry_id)
        if str(folded_entry.entry_id) != eid:
            raise ValueError(
                f"fold 目标 entry_id 不匹配：store={eid}，folded_entry={folded_entry.entry_id}"
            )
        chain = self._store.get(eid)
        if chain is None:
            raise KeyError(f"entry_id {eid} 不在 store 中，无法折叠")

        before_snapshot = chain.snapshot()
        effect = buffer.stage(
            effect_type=EffectType.MEMORY_ENTRY_FOLD,
            agent_id=self._agent_id,
            resource=f"memory:{self._agent_id}:{eid}",
            data={"entry_id": eid, "fold_to_version": folded_entry.version},
        )
        effect.invert_data["memory_before"] = (
            eid,
            [e.model_dump() for e in before_snapshot],
        )
        # 以折叠后条目替换版本链
        self._store[eid] = VersionChain(folded_entry)
        return effect

    # ------------------------------------------------------------------
    # 回滚支持（由 TransactionBuffer 的调用方在 rollback 后调用）
    # ------------------------------------------------------------------

    def rollback_effect(self, effect: StagedEffect) -> None:
        """根据 effect 的 invert_data 回滚对 store 的改动。

        应在 TransactionBuffer.rollback() 之后、按逆序调用（最后 apply
        的 effect 最先回滚）。
        """
        if effect.effect_type not in {
            EffectType.MEMORY_ENTRY_WRITE,
            EffectType.MEMORY_ENTRY_EVICT,
            EffectType.MEMORY_ENTRY_FOLD,
        }:
            return

        memory_before = effect.invert_data.get("memory_before")
        if memory_before is None:
            return

        eid, chain_dump = memory_before

        if chain_dump is None:
            # MEMORY_ENTRY_WRITE 新建条目的回滚：移除
            self._store.pop(eid, None)
        else:
            # 恢复旧版本链
            from my_team.models.memory import MemoryEntry as ME

            chain_list = [ME.model_validate(d) for d in chain_dump]
            restored = VersionChain(chain_list[0])
            for entry in chain_list[1:]:
                restored.append_version(entry)
            self._store[eid] = restored

    # ------------------------------------------------------------------
    # 外加载投影接口（projection-only，不写 store）
    # ------------------------------------------------------------------

    @staticmethod
    def project_injected(entry: MemoryEntry) -> MemoryEntry:
        """返回外加载条目的只读视图（不修改、不写入 store）。

        外加载条目的结构性保证：entry_id 不在 store 中 ⇒ 不可写。
        本函数仅是语义标记，用法：
            view = AgentMemory.project_injected(external_entry)
            # view 是外加载快照，不在 store，任何 put() 都会抛 ValueError。
        """
        if entry.provenance.origin != EntryOrigin.INJECTED:
            raise ValueError("project_injected 仅用于 origin=injected 的外加载条目")
        return entry  # MemoryEntry 本身 frozen，无需拷贝

    # ------------------------------------------------------------------
    # 内部应用（不产生 effect，供 put() 调用）
    # ------------------------------------------------------------------

    def _apply_write(self, entry: MemoryEntry) -> None:
        """直接写入 store（无 effect，由 put() 使用）。"""
        eid = str(entry.entry_id)
        if eid in self._store:
            self._store[eid].append_version(entry)
        else:
            if entry.version != 1:
                raise ValueError(f"新条目版本号必须为 1，实际为 {entry.version}")
            self._store[eid] = VersionChain(entry)

    def _snapshot_store(self) -> dict[str, list[dict[str, Any]]]:
        """导出当前 store 快照（调试/审计用）。"""
        return {eid: [e.model_dump() for e in chain.history] for eid, chain in self._store.items()}

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        return f"AgentMemory(agent_id={self._agent_id!r}, entries={len(self._store)})"
