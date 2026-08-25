"""N4-2 召回引擎测试。

覆盖要点（对应任务范围验收项）：
1. 关键词命中 top-k（KeywordRecallBackend + RecallEngine.recall）；
2. 可控查询词持久影响召回（persistent_query_terms 跨调用保持）；
3. 主动回忆 intent 语义（temp_overrides 写入 → 下 tick 消费 → 清空）；
4. 触发器索引随条目变更同步（on_put / on_evict / on_fold）；
5. 召回面 = 触发器列表可审计（audit_triggers / entry_triggers）；
6. top-k 排序（trigger_score 降序）；
7. effect 可回滚（MEMORY_RECALL_CONFIG / MEMORY_RECALL INVERT_CONTRACT）。
"""

from __future__ import annotations

import uuid

from my_team.memory_recall import (
    KeywordRecallBackend,
    RecallConfig,
    RecallEngine,
    RecallIndex,
    _source_tag_for,
)
from my_team.memory_store import AgentMemory
from my_team.models.intent import (
    IntentType,
    MemoryRecallConfigIntent,
    MemoryRecallIntent,
)
from my_team.models.memory import (
    MemoryEntryType,
    make_person_entry,
    make_skill_entry,
    make_task_entry,
)
from my_team.transaction import (
    INVERT_CONTRACT,
    EffectType,
    TransactionBuffer,
)

# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_store_engine() -> tuple[AgentMemory, RecallEngine]:
    """创建 AgentMemory + RecallEngine 对（引擎索引与 store 手动同步）。"""
    store = AgentMemory(agent_id="agent.test")
    engine = RecallEngine()
    return store, engine


def _put_and_sync(store: AgentMemory, engine: RecallEngine, entry, buffer: TransactionBuffer):
    """写入条目并同步索引。"""
    store.put(entry, buffer)
    engine.sync_put(entry)
    return entry


# ---------------------------------------------------------------------------
# 1. RecallIndex 基础：触发器索引 CRUD 同步
# ---------------------------------------------------------------------------


class TestRecallIndex:
    """触发器索引随条目变更同步（审计项：召回面 = 触发器列表）。"""

    def test_on_put_registers_triggers(self) -> None:
        """写入条目后，memory_points 和 title 均入索引。"""
        idx = RecallIndex()
        entry = make_skill_entry(
            title="退款处理SOP",
            sop_text="退款流程",
            memory_points=["退款", "客户投诉"],
        )
        idx.on_put(entry)

        eid = str(entry.entry_id)
        assert eid in idx.lookup("退款处理sop")
        assert eid in idx.lookup("退款")
        assert eid in idx.lookup("客户投诉")

    def test_on_put_version_update_replaces_triggers(self) -> None:
        """追加新版本时，索引以新版本触发器为准（旧触发器移除）。"""
        idx = RecallIndex()
        eid = uuid.uuid4()
        v1 = make_skill_entry(
            title="旧标题",
            sop_text="x",
            memory_points=["旧关键词"],
            entry_id=eid,
        )
        v2 = make_skill_entry(
            title="新标题",
            sop_text="x",
            memory_points=["新关键词"],
            entry_id=eid,
            version=2,
        )
        idx.on_put(v1)
        idx.on_put(v2)

        eid_str = str(eid)
        # 旧触发器已移除
        assert eid_str not in idx.lookup("旧关键词")
        assert eid_str not in idx.lookup("旧标题")
        # 新触发器已注册
        assert eid_str in idx.lookup("新关键词")
        assert eid_str in idx.lookup("新标题")

    def test_on_evict_removes_triggers(self) -> None:
        """撤出条目后，触发器从索引中移除。"""
        idx = RecallIndex()
        entry = make_task_entry(
            title="任务A",
            notes="",
            memory_points=["任务A", "紧急"],
        )
        idx.on_put(entry)
        idx.on_evict(entry.entry_id)

        eid = str(entry.entry_id)
        assert eid not in idx.lookup("任务a")
        assert eid not in idx.lookup("紧急")
        assert eid not in idx.entry_triggers(entry.entry_id)

    def test_on_fold_updates_triggers(self) -> None:
        """折叠后索引以折叠版触发器为准。"""
        idx = RecallIndex()
        eid = uuid.uuid4()
        original = make_task_entry(
            title="原始任务",
            notes="",
            memory_points=["原始"],
            entry_id=eid,
        )
        folded = make_task_entry(
            title="折叠任务",
            notes="",
            memory_points=["折叠后"],
            entry_id=eid,
            version=3,
        )
        idx.on_put(original)
        idx.on_fold(folded)

        eid_str = str(eid)
        assert eid_str not in idx.lookup("原始")
        assert eid_str in idx.lookup("折叠后")
        assert eid_str in idx.lookup("折叠任务")

    def test_audit_triggers_shows_full_index(self) -> None:
        """audit_triggers 导出完整触发器映射（召回面可审计）。"""
        idx = RecallIndex()
        e1 = make_skill_entry(title="技能A", sop_text="x", memory_points=["关键词1"])
        e2 = make_task_entry(title="任务B", notes="", memory_points=["关键词2"])
        idx.on_put(e1)
        idx.on_put(e2)

        audit = idx.audit_triggers()
        # 所有触发器均在审计结果中
        assert "关键词1" in audit
        assert "关键词2" in audit
        assert "技能a" in audit
        assert "任务b" in audit

    def test_entry_triggers_returns_current_version(self) -> None:
        """entry_triggers 返回当前版本触发器集合（审计）。"""
        idx = RecallIndex()
        eid = uuid.uuid4()
        entry = make_skill_entry(
            title="SOP标题",
            sop_text="x",
            memory_points=["触发器A", "触发器B"],
            entry_id=eid,
        )
        idx.on_put(entry)

        triggers = idx.entry_triggers(eid)
        assert "触发器a" in triggers
        assert "触发器b" in triggers
        assert "sop标题" in triggers

    def test_content_not_in_index(self) -> None:
        """内容字段（sop_text/notes 等）不入索引——召回面仅限触发器列表。"""
        idx = RecallIndex()
        entry = make_skill_entry(
            title="标题",
            sop_text="这段内容不应该出现在索引里",
            memory_points=["触发词"],
        )
        idx.on_put(entry)

        audit = idx.audit_triggers()
        # 内容不入索引
        assert "这段内容不应该出现在索引里" not in audit
        assert "这段内容不应该出现在索引里" not in idx.lookup("这段内容不应该出现在索引里")


# ---------------------------------------------------------------------------
# 2. KeywordRecallBackend：关键词/子串命中
# ---------------------------------------------------------------------------


class TestKeywordRecallBackend:
    """KeywordRecallBackend 关键词命中与子串匹配。"""

    def _setup(self):
        store = AgentMemory(agent_id="agent.test")
        idx = RecallIndex()
        buf = TransactionBuffer()
        backend = KeywordRecallBackend()

        e_refund = make_skill_entry(
            title="退款处理SOP",
            sop_text="退款流程规范",
            memory_points=["退款", "退款申请", "客户退款"],
        )
        e_complaint = make_task_entry(
            title="投诉处理",
            notes="",
            memory_points=["客户投诉", "投诉升级"],
        )
        e_unrelated = make_person_entry(
            title="合作伙伴档案",
            profile="外部合作方",
            memory_points=["合作方", "供应商"],
        )

        for e in [e_refund, e_complaint, e_unrelated]:
            store.put(e, buf)
            idx.on_put(e)

        return store, idx, backend, e_refund, e_complaint, e_unrelated

    def test_exact_keyword_hit(self) -> None:
        """精确关键词命中。"""
        store, idx, backend, e_refund, _, _ = self._setup()
        results = backend.query(["退款"], store, idx)
        eids = {r.entry_id for r in results}
        assert str(e_refund.entry_id) in eids

    def test_substring_hit(self) -> None:
        """子串匹配：查询词是触发器的子串。"""
        store, idx, backend, e_refund, _, _ = self._setup()
        # "退款" 是 "退款申请" 的子串
        results = backend.query(["退款申请"], store, idx)
        eids = {r.entry_id for r in results}
        assert str(e_refund.entry_id) in eids

    def test_multiple_terms_hit_multiple_entries(self) -> None:
        """多词查询命中多条目。"""
        store, idx, backend, e_refund, e_complaint, _ = self._setup()
        results = backend.query(["退款", "客户投诉"], store, idx)
        eids = {r.entry_id for r in results}
        assert str(e_refund.entry_id) in eids
        assert str(e_complaint.entry_id) in eids

    def test_no_hit_for_content_words(self) -> None:
        """内容词（sop_text/notes）不在召回面，不命中。"""
        store, idx, backend, _, _, _ = self._setup()
        results = backend.query(["退款流程规范"], store, idx)
        # 不应命中任何条目（内容不入索引）
        assert results == []

    def test_filter_types(self) -> None:
        """filter_types 限制召回类型。"""
        store, idx, backend, e_refund, e_complaint, _ = self._setup()
        results = backend.query(
            ["退款", "客户投诉"], store, idx, filter_types=[MemoryEntryType.SKILL]
        )
        eids = {r.entry_id for r in results}
        assert str(e_refund.entry_id) in eids
        assert str(e_complaint.entry_id) not in eids  # task 被过滤

    def test_unrelated_entry_not_hit(self) -> None:
        """无关条目不命中。"""
        store, idx, backend, _, _, e_unrelated = self._setup()
        results = backend.query(["退款", "投诉"], store, idx)
        eids = {r.entry_id for r in results}
        assert str(e_unrelated.entry_id) not in eids

    def test_trigger_score_equals_matched_term_count(self) -> None:
        """trigger_score = 命中查询词数。"""
        store, idx, backend, e_refund, _, _ = self._setup()
        # e_refund memory_points: 退款, 退款申请, 客户退款 → 三词均命中
        results = backend.query(["退款", "退款申请", "客户退款"], store, idx)
        refund_result = next(r for r in results if r.entry_id == str(e_refund.entry_id))
        assert refund_result.trigger_score == 3


# ---------------------------------------------------------------------------
# 3. RecallEngine：top-k 排序与三路词合并
# ---------------------------------------------------------------------------


class TestRecallEngine:
    """RecallEngine top-k 排序 + 三路词合并。"""

    def _make_env(self):
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        # 3 条目，触发词覆盖层次不同
        e_high = make_skill_entry(
            title="高优先级技能",
            sop_text="x",
            memory_points=["关键词A", "关键词B", "关键词C"],
        )
        e_mid = make_skill_entry(
            title="中优先级技能",
            sop_text="x",
            memory_points=["关键词A", "关键词B"],
        )
        e_low = make_task_entry(
            title="低优先级任务",
            notes="",
            memory_points=["关键词A"],
        )

        for e in [e_high, e_mid, e_low]:
            store.put(e, buf)
            engine.sync_put(e)

        return store, engine, config, buf, e_high, e_mid, e_low

    def test_top_k_descending_trigger_score(self) -> None:
        """top-k 按 trigger_score 降序排列。"""
        store, engine, config, _, e_high, e_mid, e_low = self._make_env()
        results = engine.recall(
            store, config,
            contextual_terms=["关键词A", "关键词B", "关键词C"],
            top_k=3,
        )
        assert len(results) == 3
        assert results[0].entry_id == str(e_high.entry_id)
        assert results[1].entry_id == str(e_mid.entry_id)
        assert results[2].entry_id == str(e_low.entry_id)

    def test_top_k_limits_results(self) -> None:
        """top_k 参数限制结果数量。"""
        store, engine, config, _, *_ = self._make_env()
        results = engine.recall(
            store, config,
            contextual_terms=["关键词A", "关键词B", "关键词C"],
            top_k=2,
        )
        assert len(results) == 2

    def test_source_tag_assigned(self) -> None:
        """召回候选带正确 source_tag。"""
        store, engine, config, _, e_high, e_mid, e_low = self._make_env()
        results = engine.recall(
            store, config,
            contextual_terms=["关键词A"],
            top_k=10,
        )
        for r in results:
            if r.entry.type == MemoryEntryType.SKILL:
                assert r.source_tag == "[SKILL_INSTRUCTION]"
            elif r.entry.type == MemoryEntryType.TASK:
                assert r.source_tag == "[TASK_CONTEXT]"

    def test_empty_terms_returns_empty(self) -> None:
        """无查询词时返回空列表。"""
        store, engine, config, _, *_ = self._make_env()
        results = engine.recall(store, config, contextual_terms=[], top_k=10)
        assert results == []

    def test_three_way_merge_deduplicates(self) -> None:
        """三路词合并：重复词只统计一次。"""
        store, engine, config, _, e_high, e_mid, e_low = self._make_env()
        config.persistent_query_terms = ["关键词A"]  # 与 contextual_terms 重复
        results = engine.recall(
            store, config,
            contextual_terms=["关键词A"],
            top_k=10,
        )
        # "关键词A" 被合并为一词，trigger_score 不会因重复词虚高
        low_result = next((r for r in results if r.entry_id == str(e_low.entry_id)), None)
        assert low_result is not None
        assert low_result.trigger_score == 1  # 仅命中一个去重后的词


# ---------------------------------------------------------------------------
# 4. 可控查询词持久性（MEMORY_RECALL_CONFIG effect）
# ---------------------------------------------------------------------------


class TestPersistentQueryTerms:
    """可控查询词持久影响召回。"""

    def test_persistent_terms_affect_recall(self) -> None:
        """persistent_query_terms 在无上下文词时仍能触发召回。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        entry = make_skill_entry(
            title="持久技能",
            sop_text="x",
            memory_points=["持久关键词"],
        )
        store.put(entry, buf)
        engine.sync_put(entry)

        # 不提供 contextual_terms，只靠 persistent_query_terms
        config.persistent_query_terms = ["持久关键词"]
        results = engine.recall(store, config, contextual_terms=[], top_k=5)
        assert any(r.entry_id == str(entry.entry_id) for r in results)

    def test_apply_recall_config_effect_persists(self) -> None:
        """apply_recall_config_effect 更新持久查询词，跨调用保持。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        entry = make_skill_entry(
            title="新技能",
            sop_text="x",
            memory_points=["新词"],
        )
        store.put(entry, buf)
        engine.sync_put(entry)

        # 通过 effect 写入持久查询词
        effect = engine.apply_recall_config_effect(
            config, ["新词"], buf, "agent.test"
        )
        assert effect.effect_type == EffectType.MEMORY_RECALL_CONFIG
        assert config.persistent_query_terms == ["新词"]

        # 第二次召回（无上下文词）仍命中
        results = engine.recall(store, config, contextual_terms=[], top_k=5)
        assert any(r.entry_id == str(entry.entry_id) for r in results)

    def test_recall_config_effect_rollback(self) -> None:
        """MEMORY_RECALL_CONFIG effect 可回滚（恢复旧持久查询词）。"""
        config = RecallConfig(persistent_query_terms=["旧词"])
        engine = RecallEngine()
        buf = TransactionBuffer()

        effect = engine.apply_recall_config_effect(
            config, ["新词"], buf, "agent.test"
        )
        assert config.persistent_query_terms == ["新词"]

        # 回滚
        RecallEngine.rollback_recall_config_effect(config, effect)
        assert config.persistent_query_terms == ["旧词"]

    def test_recall_config_invert_contract_registered(self) -> None:
        """MEMORY_RECALL_CONFIG 已在 INVERT_CONTRACT 中注册。"""
        assert EffectType.MEMORY_RECALL_CONFIG in INVERT_CONTRACT


# ---------------------------------------------------------------------------
# 5. 主动回忆 intent 语义（temp_overrides，延迟 1 tick 生效）
# ---------------------------------------------------------------------------


class TestMemoryRecallIntent:
    """主动回忆：temp_overrides 写入 → 下 tick 消费 → 清空（一次性语义）。"""

    def test_apply_memory_recall_effect_adds_temp_terms(self) -> None:
        """apply_memory_recall_effect 写入 temp_overrides。"""
        config = RecallConfig()
        engine = RecallEngine()
        buf = TransactionBuffer()

        effect = engine.apply_memory_recall_effect(
            config, ["临时词A", "临时词B"], buf, "agent.test"
        )
        assert effect.effect_type == EffectType.MEMORY_RECALL
        assert "临时词A" in config.temp_overrides
        assert "临时词B" in config.temp_overrides

    def test_temp_overrides_consumed_after_recall(self) -> None:
        """recall() 消费 temp_overrides 后清空（一次性语义）。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        entry = make_skill_entry(
            title="临时技能",
            sop_text="x",
            memory_points=["临时词"],
        )
        store.put(entry, buf)
        engine.sync_put(entry)

        config.temp_overrides = ["临时词"]
        assert config.temp_overrides  # 写入后存在

        # 模拟"下 tick 的 Observe/召回阶段"消费
        results = engine.recall(
            store, config, contextual_terms=[], top_k=5, consume_temp_overrides=True
        )
        assert any(r.entry_id == str(entry.entry_id) for r in results)

        # 消费后清空
        assert config.temp_overrides == []

    def test_temp_overrides_not_consumed_when_flag_false(self) -> None:
        """consume_temp_overrides=False 时不清空（审计/测试场景）。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        config = RecallConfig(temp_overrides=["词A"])

        engine.recall(store, config, contextual_terms=[], top_k=5, consume_temp_overrides=False)
        assert config.temp_overrides == ["词A"]  # 未清空

    def test_temp_overrides_are_one_shot(self) -> None:
        """temp_overrides 一次性：消费后再次召回不再命中（延迟 1 tick 语义）。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        entry = make_skill_entry(
            title="一次性技能",
            sop_text="x",
            memory_points=["一次性词"],
        )
        store.put(entry, buf)
        engine.sync_put(entry)

        config.temp_overrides = ["一次性词"]

        # 第一次 recall（模拟 tick N 的召回）：命中，消费
        r1 = engine.recall(store, config, contextual_terms=[], top_k=5)
        assert any(r.entry_id == str(entry.entry_id) for r in r1)
        assert config.temp_overrides == []

        # 第二次 recall（模拟 tick N+1 的召回）：不再命中
        r2 = engine.recall(store, config, contextual_terms=[], top_k=5)
        assert not any(r.entry_id == str(entry.entry_id) for r in r2)

    def test_memory_recall_effect_rollback(self) -> None:
        """MEMORY_RECALL effect 可回滚（移除写入的临时词）。"""
        config = RecallConfig()
        engine = RecallEngine()
        buf = TransactionBuffer()

        effect = engine.apply_memory_recall_effect(
            config, ["临时词"], buf, "agent.test"
        )
        assert "临时词" in config.temp_overrides

        # 回滚
        RecallEngine.rollback_memory_recall_effect(config, effect)
        assert "临时词" not in config.temp_overrides

    def test_memory_recall_invert_contract_registered(self) -> None:
        """MEMORY_RECALL 已在 INVERT_CONTRACT 中注册。"""
        assert EffectType.MEMORY_RECALL in INVERT_CONTRACT

    def test_memory_recall_intent_type(self) -> None:
        """MemoryRecallIntent 使用正确的 intent_type。"""
        intent = MemoryRecallIntent(
            agent_id="agent.test",
            temp_query_terms=["词A"],
        )
        assert intent.intent_type == IntentType.MEMORY_RECALL
        assert intent.temp_query_terms == ["词A"]

    def test_memory_recall_config_intent_type(self) -> None:
        """MemoryRecallConfigIntent 使用正确的 intent_type。"""
        intent = MemoryRecallConfigIntent(
            agent_id="agent.test",
            persistent_query_terms=["持久词"],
        )
        assert intent.intent_type == IntentType.MEMORY_RECALL_CONFIG
        assert intent.persistent_query_terms == ["持久词"]


# ---------------------------------------------------------------------------
# 6. 触发器索引随条目变更同步（通过 RecallEngine 便捷方法）
# ---------------------------------------------------------------------------


class TestIndexSyncViaEngine:
    """RecallEngine.sync_put/evict/fold 与 AgentMemory 操作的同步正确性。"""

    def test_sync_put_new_entry_indexed(self) -> None:
        """sync_put 后新条目可被召回。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        entry = make_task_entry(title="新任务", notes="", memory_points=["新任务词"])
        store.put(entry, buf)
        engine.sync_put(entry)

        results = engine.recall(store, config, contextual_terms=["新任务词"], top_k=5)
        assert any(r.entry_id == str(entry.entry_id) for r in results)

    def test_sync_evict_removes_from_recall(self) -> None:
        """sync_evict 后条目从召回结果中消失。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        entry = make_task_entry(title="待撤出", notes="", memory_points=["撤出词"])
        store.put(entry, buf)
        engine.sync_put(entry)

        # 撤出
        store.evict(entry.entry_id, buf)
        engine.sync_evict(entry.entry_id)

        results = engine.recall(store, config, contextual_terms=["撤出词"], top_k=5)
        assert not any(r.entry_id == str(entry.entry_id) for r in results)

    def test_sync_fold_updates_triggers(self) -> None:
        """sync_fold 后，折叠后触发器生效，旧触发器失效。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        eid = uuid.uuid4()
        v1 = make_skill_entry(
            title="原始技能",
            sop_text="x",
            memory_points=["原始触发词"],
            entry_id=eid,
        )
        store.put(v1, buf)
        engine.sync_put(v1)

        # 折叠版本
        v_folded = make_skill_entry(
            title="折叠后技能",
            sop_text="x",
            memory_points=["折叠后触发词"],
            entry_id=eid,
            version=5,
        )
        store.fold(eid, v_folded, buf)
        engine.sync_fold(v_folded)

        # 旧触发词不再命中
        r_old = engine.recall(store, config, contextual_terms=["原始触发词"], top_k=5)
        assert not any(r.entry_id == str(eid) for r in r_old)

        # 新触发词命中
        r_new = engine.recall(store, config, contextual_terms=["折叠后触发词"], top_k=5)
        assert any(r.entry_id == str(eid) for r in r_new)

    def test_multiple_entries_distinct_triggers(self) -> None:
        """多条目触发词互不干扰，各自独立命中。"""
        store = AgentMemory(agent_id="agent.test")
        engine = RecallEngine()
        buf = TransactionBuffer()
        config = RecallConfig()

        entries = []
        for i in range(5):
            e = make_skill_entry(
                title=f"技能{i}",
                sop_text="x",
                memory_points=[f"专属词{i}"],
            )
            store.put(e, buf)
            engine.sync_put(e)
            entries.append(e)

        for i, e in enumerate(entries):
            results = engine.recall(store, config, contextual_terms=[f"专属词{i}"], top_k=5)
            hit_ids = {r.entry_id for r in results}
            assert str(e.entry_id) in hit_ids
            # 其他条目不命中
            for j, other in enumerate(entries):
                if j != i:
                    assert str(other.entry_id) not in hit_ids


# ---------------------------------------------------------------------------
# 7. RecallBackend 协议检查
# ---------------------------------------------------------------------------


class TestRecallBackendProtocol:
    """KeywordRecallBackend 实现 RecallBackend 协议。"""

    def test_keyword_backend_is_recall_backend(self) -> None:
        """KeywordRecallBackend 满足 RecallBackend Protocol。"""
        from my_team.memory_recall import RecallBackend
        backend = KeywordRecallBackend()
        assert isinstance(backend, RecallBackend)

    def test_source_tag_mapping(self) -> None:
        """各类型条目的 source_tag 正确映射。"""
        skill = make_skill_entry(title="s", sop_text="x")
        task = make_task_entry(title="t", notes="")
        person = make_person_entry(title="p")

        assert _source_tag_for(skill) == "[SKILL_INSTRUCTION]"
        assert _source_tag_for(task) == "[TASK_CONTEXT]"
        assert _source_tag_for(person) == "[PERSON_PROFILE]"
