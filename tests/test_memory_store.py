"""N4-1 记忆存储测试（AgentMemory + VersionChain + effect 回滚）。

覆盖要点：
- 版本链不可变（append-only，版本号严格递增）；
- 外加载条目不在 store（结构性防改写）；
- put/get/evict/fold/rollback_effect；
- EffectType.MEMORY_ENTRY_WRITE/EVICT/FOLD 入 INVERT_CONTRACT；
- effect 可回滚（invert_data 记旧版本链）。
"""

from __future__ import annotations

import uuid

import pytest

from my_team.memory_store import AgentMemory, VersionChain
from my_team.models.memory import (
    EntryOrigin,
    EntryProvenance,
    InjectionRef,
    MemoryEntry,
    MemoryEntryType,
    SkillContent,
    TaskContent,
    make_skill_entry,
    make_task_entry,
)
from my_team.transaction import (
    INVERT_CONTRACT,
    EffectType,
    TransactionBuffer,
)

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_store() -> AgentMemory:
    return AgentMemory(agent_id="agent.test")


def _make_buffer() -> TransactionBuffer:
    return TransactionBuffer()


def _v1_task(entry_id: uuid.UUID | None = None) -> MemoryEntry:
    return make_task_entry(title="任务1", notes="处理退款", entry_id=entry_id)


def _v2_task(entry_id: uuid.UUID) -> MemoryEntry:
    return MemoryEntry(
        entry_id=entry_id,
        type=MemoryEntryType.TASK,
        title="任务1 v2",
        content=TaskContent(notes="更新：已审批"),
        version=2,
    )


def _v3_task(entry_id: uuid.UUID) -> MemoryEntry:
    return MemoryEntry(
        entry_id=entry_id,
        type=MemoryEntryType.TASK,
        title="任务1 v3",
        content=TaskContent(notes="完成"),
        version=3,
    )


# ---------------------------------------------------------------------------
# VersionChain 测试
# ---------------------------------------------------------------------------


class TestVersionChain:
    """版本链不可变性与追加语义。"""

    def test_initial_version_is_accessible(self) -> None:
        """初始条目可通过 current 访问。"""
        e = _v1_task()
        chain = VersionChain(e)
        assert chain.current == e
        assert chain.version_count == 1

    def test_append_version_succeeds_with_correct_version(self) -> None:
        """版本号严格递增时追加成功。"""
        eid = uuid.uuid4()
        v1 = _v1_task(eid)
        v2 = _v2_task(eid)
        chain = VersionChain(v1)
        chain.append_version(v2)
        assert chain.current == v2
        assert chain.version_count == 2

    def test_append_wrong_version_raises(self) -> None:
        """版本号不递增时抛 ValueError。"""
        eid = uuid.uuid4()
        v1 = _v1_task(eid)
        # version=3 跳过了 2
        v3 = _v3_task(eid)
        chain = VersionChain(v1)
        with pytest.raises(ValueError, match="严格递增"):
            chain.append_version(v3)

    def test_append_mismatched_entry_id_raises(self) -> None:
        """entry_id 不匹配时抛 ValueError。"""
        eid1 = uuid.uuid4()
        eid2 = uuid.uuid4()
        chain = VersionChain(_v1_task(eid1))
        v2 = _v2_task(eid2)  # 不同 entry_id
        with pytest.raises(ValueError, match="entry_id"):
            chain.append_version(v2)

    def test_history_is_copy_immutable(self) -> None:
        """history 返回副本，修改不影响内部状态。"""
        eid = uuid.uuid4()
        chain = VersionChain(_v1_task(eid))
        chain.append_version(_v2_task(eid))
        hist = chain.history
        hist.pop()
        assert chain.version_count == 2  # 内部未受影响

    def test_pop_latest_version_removes_it(self) -> None:
        """pop_latest_version 移除最新版本。"""
        eid = uuid.uuid4()
        v1 = _v1_task(eid)
        v2 = _v2_task(eid)
        chain = VersionChain(v1)
        chain.append_version(v2)
        popped = chain.pop_latest_version()
        assert popped == v2
        assert chain.current == v1

    def test_pop_only_version_raises(self) -> None:
        """只有一个版本时 pop → ValueError。"""
        chain = VersionChain(_v1_task())
        with pytest.raises(ValueError, match="只剩一个版本"):
            chain.pop_latest_version()


# ---------------------------------------------------------------------------
# AgentMemory 基本操作
# ---------------------------------------------------------------------------


class TestAgentMemoryBasics:
    """get/put/list/contains 基本操作。"""

    def test_empty_store_get_returns_none(self) -> None:
        """空 store get → None。"""
        store = _make_store()
        assert store.get(uuid.uuid4()) is None

    def test_put_and_get(self) -> None:
        """put 后 get 返回最新版本。"""
        store = _make_store()
        buf = _make_buffer()
        e = _v1_task()
        store.put(e, buf)
        assert store.get(e.entry_id) == e

    def test_put_second_version_returns_latest(self) -> None:
        """追加第二版本后 get 返回 v2。"""
        store = _make_store()
        buf = _make_buffer()
        eid = uuid.uuid4()
        v1 = _v1_task(eid)
        v2 = _v2_task(eid)
        store.put(v1, buf)
        store.put(v2, buf)
        assert store.get(eid) == v2

    def test_list_entries_returns_latest_versions(self) -> None:
        """list_entries 返回所有条目的最新版本。"""
        store = _make_store()
        buf = _make_buffer()
        eid = uuid.uuid4()
        v1 = _v1_task(eid)
        v2 = _v2_task(eid)
        store.put(v1, buf)
        store.put(v2, buf)
        entries = store.list_entries()
        assert len(entries) == 1
        assert entries[0].version == 2

    def test_list_entries_filter_by_type(self) -> None:
        """list_entries 可按 type 过滤。"""
        store = _make_store()
        buf = _make_buffer()
        store.put(_v1_task(), buf)
        store.put(make_skill_entry(title="s", sop_text="sop"), buf)
        task_entries = store.list_entries(entry_type=MemoryEntryType.TASK)
        skill_entries = store.list_entries(entry_type=MemoryEntryType.SKILL)
        assert all(e.type == MemoryEntryType.TASK for e in task_entries)
        assert all(e.type == MemoryEntryType.SKILL for e in skill_entries)

    def test_contains_true_for_stored_entry(self) -> None:
        """store 中的条目 contains=True。"""
        store = _make_store()
        buf = _make_buffer()
        e = _v1_task()
        store.put(e, buf)
        assert store.contains(e.entry_id)

    def test_contains_false_for_unknown_entry(self) -> None:
        """未存储的 entry_id contains=False。"""
        store = _make_store()
        assert not store.contains(uuid.uuid4())

    def test_get_chain_returns_all_versions(self) -> None:
        """get_chain 返回完整版本链。"""
        store = _make_store()
        buf = _make_buffer()
        eid = uuid.uuid4()
        store.put(_v1_task(eid), buf)
        store.put(_v2_task(eid), buf)
        store.put(_v3_task(eid), buf)
        chain = store.get_chain(eid)
        assert [e.version for e in chain] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 外加载条目不入库（结构性防改写）
# ---------------------------------------------------------------------------


class TestExternalEntryNotInStore:
    """外加载条目（origin=injected）不写入 store——结构性保证。"""

    def test_injected_entry_cannot_be_put_to_store(self) -> None:
        """put injected 条目 → ValueError（结构性防改写）。"""
        store = _make_store()
        buf = _make_buffer()
        prov = EntryProvenance(
            origin=EntryOrigin.INJECTED,
            injection_ref=InjectionRef(
                position_id="pos.hr",
                entity_id="ent.jd",
                snapshot_version=1,
            ),
        )
        e = MemoryEntry(
            type=MemoryEntryType.TASK,
            title="外加载任务描述",
            content=TaskContent(notes="岗位 JD"),
            provenance=prov,
        )
        with pytest.raises(ValueError, match="外加载条目"):
            store.put(e, buf)

    def test_injected_entry_not_in_store_after_projection(self) -> None:
        """project_injected 返回视图，但 entry_id 不在 store 中。"""
        store = _make_store()
        prov = EntryProvenance(
            origin=EntryOrigin.INJECTED,
            injection_ref=InjectionRef(entity_id="ent.jd"),
        )
        e = MemoryEntry(
            type=MemoryEntryType.SKILL,
            title="外加载 SOP",
            content=SkillContent(sop_text="JD 规定的 SOP"),
            provenance=prov,
        )
        view = AgentMemory.project_injected(e)
        # view 等同于 e（frozen，无需拷贝）
        assert view is e
        # store 中不存在该 entry_id
        assert not store.contains(e.entry_id)

    def test_project_own_entry_raises(self) -> None:
        """project_injected 传入 own 条目 → ValueError。"""
        e = _v1_task()
        with pytest.raises(ValueError, match="origin=injected"):
            AgentMemory.project_injected(e)


# ---------------------------------------------------------------------------
# evict 操作
# ---------------------------------------------------------------------------


class TestEvict:
    """evict：从 store 中移除条目。"""

    def test_evict_existing_entry_removes_it(self) -> None:
        """evict 已存在条目 → store 中不再含有。"""
        store = _make_store()
        buf = _make_buffer()
        e = _v1_task()
        store.put(e, buf)
        store.evict(e.entry_id, buf)
        assert not store.contains(e.entry_id)

    def test_evict_nonexistent_returns_none(self) -> None:
        """evict 不存在条目 → 返回 None（无操作）。"""
        store = _make_store()
        buf = _make_buffer()
        result = store.evict(uuid.uuid4(), buf)
        assert result is None

    def test_evict_produces_effect(self) -> None:
        """evict 产生 MEMORY_ENTRY_EVICT effect。"""
        store = _make_store()
        buf = _make_buffer()
        e = _v1_task()
        store.put(e, buf)
        effect = store.evict(e.entry_id, buf)
        assert effect is not None
        assert effect.effect_type == EffectType.MEMORY_ENTRY_EVICT


# ---------------------------------------------------------------------------
# fold 操作
# ---------------------------------------------------------------------------


class TestFold:
    """fold：折叠（压缩）版本链。"""

    def test_fold_replaces_version_chain(self) -> None:
        """fold 后 get 返回折叠后的条目。"""
        store = _make_store()
        buf = _make_buffer()
        eid = uuid.uuid4()
        store.put(_v1_task(eid), buf)
        store.put(_v2_task(eid), buf)
        # 折叠为单一版本（v3）
        folded = _v3_task(eid)
        store.fold(eid, folded, buf)
        assert store.get(eid) == folded
        assert len(store.get_chain(eid)) == 1

    def test_fold_nonexistent_raises(self) -> None:
        """fold 不存在的 entry_id → KeyError。"""
        store = _make_store()
        buf = _make_buffer()
        eid = uuid.uuid4()
        with pytest.raises(KeyError):
            store.fold(eid, _v1_task(eid), buf)


# ---------------------------------------------------------------------------
# effect 可回滚（invert_data）
# ---------------------------------------------------------------------------


class TestEffectRollback:
    """记忆 effect 携带 invert_data，支持回滚。"""

    def test_write_effect_has_invert_data(self) -> None:
        """MEMORY_ENTRY_WRITE effect 包含 invert_data。"""
        store = _make_store()
        buf = _make_buffer()
        e = _v1_task()
        effect = store.put(e, buf)
        assert "memory_before" in effect.invert_data

    def test_new_entry_write_invert_data_is_none(self) -> None:
        """新建条目时 invert_data['memory_before'][1]=None（回滚=移除）。"""
        store = _make_store()
        buf = _make_buffer()
        e = _v1_task()
        effect = store.put(e, buf)
        _, chain_dump = effect.invert_data["memory_before"]
        assert chain_dump is None  # 新建条目，回滚=移除

    def test_rollback_new_entry_removes_it(self) -> None:
        """回滚 MEMORY_ENTRY_WRITE（新建）→ 条目从 store 中移除。"""
        store = _make_store()
        buf = _make_buffer()
        e = _v1_task()
        effect = store.put(e, buf)
        assert store.contains(e.entry_id)
        store.rollback_effect(effect)
        assert not store.contains(e.entry_id)

    def test_rollback_version_append_restores_previous(self) -> None:
        """回滚 MEMORY_ENTRY_WRITE（追加版本）→ 恢复为 v1。"""
        store = _make_store()
        buf = _make_buffer()
        eid = uuid.uuid4()
        v1 = _v1_task(eid)
        v2 = _v2_task(eid)
        # put v1
        store.put(v1, buf)
        # put v2
        effect_v2 = store.put(v2, buf)
        assert store.get(eid).version == 2
        # 回滚 v2 的写入
        store.rollback_effect(effect_v2)
        assert store.get(eid).version == 1

    def test_rollback_evict_restores_entry(self) -> None:
        """回滚 MEMORY_ENTRY_EVICT → 条目被恢复到 store。"""
        store = _make_store()
        buf = _make_buffer()
        e = _v1_task()
        store.put(e, buf)
        evict_effect = store.evict(e.entry_id, buf)
        assert not store.contains(e.entry_id)
        store.rollback_effect(evict_effect)
        assert store.contains(e.entry_id)
        assert store.get(e.entry_id).version == 1

    def test_rollback_fold_restores_full_chain(self) -> None:
        """回滚 MEMORY_ENTRY_FOLD → 恢复折叠前的完整版本链。"""
        store = _make_store()
        buf = _make_buffer()
        eid = uuid.uuid4()
        store.put(_v1_task(eid), buf)
        store.put(_v2_task(eid), buf)
        # fold 为 v3
        folded = _v3_task(eid)
        fold_effect = store.fold(eid, folded, buf)
        assert len(store.get_chain(eid)) == 1
        # 回滚 fold
        store.rollback_effect(fold_effect)
        chain = store.get_chain(eid)
        assert [e.version for e in chain] == [1, 2]


# ---------------------------------------------------------------------------
# EffectType & INVERT_CONTRACT 完整性
# ---------------------------------------------------------------------------


class TestMemoryEffectTypeInContract:
    """记忆 effect 类型必须在 INVERT_CONTRACT 中声明。"""

    def test_memory_entry_write_in_contract(self) -> None:
        """MEMORY_ENTRY_WRITE 在 INVERT_CONTRACT 中。"""
        assert EffectType.MEMORY_ENTRY_WRITE in INVERT_CONTRACT

    def test_memory_entry_evict_in_contract(self) -> None:
        """MEMORY_ENTRY_EVICT 在 INVERT_CONTRACT 中。"""
        assert EffectType.MEMORY_ENTRY_EVICT in INVERT_CONTRACT

    def test_memory_entry_fold_in_contract(self) -> None:
        """MEMORY_ENTRY_FOLD 在 INVERT_CONTRACT 中。"""
        assert EffectType.MEMORY_ENTRY_FOLD in INVERT_CONTRACT

    def test_all_effect_types_in_contract(self) -> None:
        """所有 EffectType 枚举值都在 INVERT_CONTRACT——无遗漏。"""
        for etype in EffectType:
            assert etype in INVERT_CONTRACT, f"{etype.value} 缺失于 INVERT_CONTRACT"
