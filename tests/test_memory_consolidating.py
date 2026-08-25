"""N4-4 整理模式 CONSOLIDATING 测试。

覆盖要点（对应任务范围验收项）：
1. 触发：预算标志（pending_consolidation / fixed_usage_ratio）+ 主动
   intent（MemoryConsolidateIntent enter，不限于预算满）；
2. hysteresis 进出阈值（进 90% / 出 80%）；
3. 工具面收窄（CONSOLIDATING 下授权集 = 记忆工具集，执行侧 + 渲染侧）；
4. 动作入 Journal（memory_fold/promote/edit/retag/evict/pin 全部为
   Journal effect，可审计）；
5. 退出恢复 resume_phase（agent 自决 exit 或预算回落阈值下）；
6. 结构化摘要含反思/经验/流程优化/记忆链接字段（确定性解析，fake_llm
   可测）；
7. provenance 关联（摘要条目 consolidation_origin；memory_promote 带
   TaskResultRef 结果 provenance）。
"""

from __future__ import annotations

import json
import uuid

from my_team.agent_runtime import (
    ActionResult,
    AgentAction,
    AgentObservation,
    ToolContext,
    ToolRegistry,
)
from my_team.agent_tree import AgentTree
from my_team.consolidation import (
    CONSOLIDATION_DIRECTIVE,
    MEMORY_TOOL_NAMES,
    ConsolidationConfig,
    ConsolidationGate,
    ConsolidationSummary,
    make_summary_entry,
    parse_consolidation_output,
    parse_consolidation_request,
    parse_consolidation_summary,
    write_summary_entry,
)
from my_team.devices.authority import Authority, new_team_id
from my_team.devices.base import EntityKind, InjectionDecl
from my_team.llm_agent import LLMAgent
from my_team.llm_gateway import LLMGateway
from my_team.memory_recall import RecallConfig, RecallEngine
from my_team.memory_store import AgentMemory
from my_team.memory_tools import (
    make_handle_memory_edit,
    make_handle_memory_evict,
    make_handle_memory_fold,
    make_handle_memory_pin,
    make_handle_memory_promote,
    make_handle_memory_retag,
)
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import (
    IntentType,
    MemoryConsolidateIntent,
    SubmitToolRequest,
)
from my_team.models.llm import LLMProviderConfig
from my_team.models.memory import (
    MemoryEntryType,
    TaskResultRef,
    make_skill_entry,
)
from my_team.simulation import Simulation
from my_team.tool_manifest import builtin_manifests
from my_team.transaction import (
    INVERT_CONTRACT,
    EffectType,
    TransactionBuffer,
)

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _ctx(agent_id: str = "agent.test", tick: int = 1) -> ToolContext:
    return ToolContext(agent_id=agent_id, tick=tick)


def _make_env(
    agent_id: str = "agent.test",
) -> tuple[AgentMemory, RecallEngine, RecallConfig, TransactionBuffer]:
    """AgentMemory + RecallEngine + RecallConfig + TransactionBuffer 对。"""
    store = AgentMemory(agent_id=agent_id)
    engine = RecallEngine()
    config = RecallConfig()
    buffer = TransactionBuffer()
    return store, engine, config, buffer


def _put_skill(
    store: AgentMemory,
    engine: RecallEngine,
    buffer: TransactionBuffer,
    *,
    title: str = "退款处理",
    sop_text: str = "第一步联系客户，第二步核实订单",
    memory_points: list[str] | None = None,
) -> object:
    entry = make_skill_entry(
        title=title,
        sop_text=sop_text,
        memory_points=memory_points or ["退款"],
    )
    store.put(entry, buffer)
    engine.sync_put(entry)
    return entry


def _consolidation_summary_dict() -> dict:
    return {
        "reflection_and_growth": "学会了先核实再处理",
        "lessons_learned": "退款要先查订单状态",
        "process_optimization": "处理流程压缩为两步",
        "memory_links": ["退款流程", "客户投诉"],
    }


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({"agents": [
        {
            "agent_id": "agent.root",
            "display_name": "Root",
            "role": "root_decision_agent",
            "parent_id": None,
            "children": [],
            "tools": [],
            "can_delegate": True,
            "metadata": {"bootstrap": True, "mission": "Test mission"},
        },
    ]})


def _make_sim() -> Simulation:
    return Simulation(agent_tree=_make_tree())


# ===========================================================================
# 1. CONSOLIDATING 相位 + resume_phase
# ===========================================================================


class TestConsolidatingPhase:
    """ContinuationPhase.CONSOLIDATING + resume_phase 进出语义。"""

    def test_consolidating_phase_enum(self) -> None:
        assert ContinuationPhase.CONSOLIDATING == "consolidating"

    def test_enter_remembers_resume_phase(self) -> None:
        cont = AgentContinuation(agent_id="agent.test")
        cont.advance_to_waiting_tool("req.1", tick=1)
        cont.enter_consolidating(tick=2)
        assert cont.phase == ContinuationPhase.CONSOLIDATING
        assert cont.resume_phase == ContinuationPhase.WAITING_FOR_TOOL

    def test_exit_restores_resume_phase(self) -> None:
        cont = AgentContinuation(agent_id="agent.test")
        cont.advance_to_waiting_tool("req.1", tick=1)
        cont.enter_consolidating(tick=2)
        cont.exit_consolidating(tick=3)
        # 被打断的工作（等待工具结果）被恢复，下一 tick 立即续上
        assert cont.phase == ContinuationPhase.WAITING_FOR_TOOL
        assert cont.resume_phase is None

    def test_exit_without_session_is_noop(self) -> None:
        """非会话中 exit 为 no-op（resume_phase=None ⟺ 不在整理会话）。"""
        cont = AgentContinuation(agent_id="agent.test")
        cont.phase = ContinuationPhase.CONSOLIDATING  # 手工置位（无会话标记）
        cont.exit_consolidating(tick=2)
        assert cont.phase == ContinuationPhase.CONSOLIDATING  # 不变（防御）

    def test_enter_is_idempotent(self) -> None:
        cont = AgentContinuation(agent_id="agent.test")
        cont.enter_consolidating(tick=1)
        cont.enter_consolidating(tick=2)  # 重复进入 no-op
        assert cont.phase == ContinuationPhase.CONSOLIDATING
        assert cont.resume_phase == ContinuationPhase.FRESH

    def test_exit_when_not_consolidating_is_noop(self) -> None:
        cont = AgentContinuation(agent_id="agent.test")
        cont.exit_consolidating(tick=1)
        assert cont.phase == ContinuationPhase.FRESH


# ===========================================================================
# 2. 结构化摘要解析（确定性输入，fake_llm 可测）
# ===========================================================================


class TestConsolidationParsing:
    """CONSOLIDATING LLM 输出 → 结构化摘要 + exit 标志的解析。"""

    def test_parse_summary_full(self) -> None:
        content = json.dumps({"consolidation_summary": _consolidation_summary_dict()})
        summary = parse_consolidation_summary(content)
        assert summary is not None
        assert summary.reflection_and_growth == "学会了先核实再处理"
        assert summary.lessons_learned == "退款要先查订单状态"
        assert summary.process_optimization == "处理流程压缩为两步"
        assert summary.memory_links == ["退款流程", "客户投诉"]

    def test_parse_summary_fenced_json_block(self) -> None:
        content = (
            "整理完成。\n```json\n"
            + json.dumps({"consolidation_summary": _consolidation_summary_dict()})
            + "\n```\n"
        )
        summary = parse_consolidation_summary(content)
        assert summary is not None
        assert summary.reflection_and_growth

    def test_parse_summary_partial_fields_default(self) -> None:
        content = json.dumps({"consolidation_summary": {"lessons_learned": "唯一经验"}})
        summary = parse_consolidation_summary(content)
        assert summary is not None
        assert summary.lessons_learned == "唯一经验"
        assert summary.reflection_and_growth == ""  # 缺省空
        assert summary.memory_links == []

    def test_parse_no_json_returns_none(self) -> None:
        output = parse_consolidation_output("只是普通文本，没有 JSON")
        assert output.summary is None
        assert output.exit_requested is False

    def test_parse_exit_flag(self) -> None:
        content = json.dumps({"exit": True})
        output = parse_consolidation_output(content)
        assert output.summary is None
        assert output.exit_requested is True

    def test_parse_summary_with_exit(self) -> None:
        content = json.dumps({
            "consolidation_summary": _consolidation_summary_dict(),
            "exit": True,
        })
        output = parse_consolidation_output(content)
        assert output.summary is not None
        assert output.exit_requested is True

    def test_judge_reserved_fields_parsed(self) -> None:
        """JUDGE 预留字段（assigner_ref/kpi_ref）随摘要解析承载（接口预留，
        非 N5 完整闭环）。"""
        d = _consolidation_summary_dict()
        d["assigner_ref"] = "agent.assigner"
        d["kpi_ref"] = "kpi:refund_success_rate"
        summary = parse_consolidation_summary(json.dumps({"consolidation_summary": d}))
        assert summary is not None
        assert summary.assigner_ref == "agent.assigner"
        assert summary.kpi_ref == "kpi:refund_success_rate"

    def test_parse_request_marker_text(self) -> None:
        """普通模式下主动整理请求标记（文本形式）→ 主动触发。"""
        assert parse_consolidation_request("我想 memory_consolidate 一下记忆") is True
        assert parse_consolidation_request("consolidate memory now") is True

    def test_parse_request_marker_json(self) -> None:
        assert parse_consolidation_request(json.dumps({"memory_consolidate": True})) is True
        assert parse_consolidation_request("普通回复") is False

    def test_directive_mentions_required_fields(self) -> None:
        """整理指令必须约束输出契约：反思/经验/流程优化/记忆链接。"""
        assert "reflection_and_growth" in CONSOLIDATION_DIRECTIVE
        assert "lessons_learned" in CONSOLIDATION_DIRECTIVE
        assert "process_optimization" in CONSOLIDATION_DIRECTIVE
        assert "memory_links" in CONSOLIDATION_DIRECTIVE
        assert "exit" in CONSOLIDATION_DIRECTIVE


# ===========================================================================
# 3. hysteresis 进出阈值（进 90% / 出 80%）
# ===========================================================================


class TestConsolidationGate:
    """ConsolidationGate 进出判定（纯函数）。"""

    def test_enter_at_90_percent(self) -> None:
        gate = ConsolidationGate()
        assert gate.should_enter(usage_ratio=0.90) is True

    def test_no_enter_below_90(self) -> None:
        gate = ConsolidationGate()
        assert gate.should_enter(usage_ratio=0.89) is False

    def test_enter_on_budget_flag(self) -> None:
        """组装器 pending_consolidation 标志（Observe 只读）触发进入。"""
        gate = ConsolidationGate()
        assert gate.should_enter(usage_ratio=0.0, pending_consolidation=True) is True

    def test_enter_on_active_intent(self) -> None:
        """agent 主动发起（不限于预算满）触发进入。"""
        gate = ConsolidationGate()
        assert gate.should_enter(usage_ratio=0.0, active_intent=True) is True

    def test_exit_below_80(self) -> None:
        gate = ConsolidationGate()
        assert gate.should_exit(usage_ratio=0.79) is True

    def test_no_exit_between_80_and_90(self) -> None:
        """hysteresis 区间内既不进也不出（防连续 tick 抖动）。"""
        gate = ConsolidationGate()
        assert gate.should_exit(usage_ratio=0.85) is False
        assert gate.should_enter(usage_ratio=0.85) is False

    def test_self_decided_exit_any_ratio(self) -> None:
        gate = ConsolidationGate()
        assert gate.should_exit(usage_ratio=0.95, self_decided=True) is True

    def test_config_thresholds(self) -> None:
        cfg = ConsolidationConfig()
        assert cfg.enter_ratio == 0.9
        assert cfg.exit_ratio == 0.8


# ===========================================================================
# 4. 记忆工具集 handler（全部 = Journal effect）
# ===========================================================================


class TestMemoryTools:
    """memory_fold/promote/edit/retag/evict/pin 六件套。"""

    def test_fold_stages_fold_effect(self) -> None:
        store, engine, config, buffer = _make_env()
        skill = _put_skill(store, engine, buffer)
        handler = make_handle_memory_fold(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(
            context=_ctx(),
            entry_id=str(skill.entry_id),
            content_text="浓缩 SOP：核实订单后处理",
            memory_points=["退款", "核实"],
        )
        assert res.success
        assert res.data["version"] == 2
        # MEMORY_ENTRY_FOLD effect 入 Journal（可审计）
        assert any(
            e.effect_type == EffectType.MEMORY_ENTRY_FOLD
            for e in buffer.get_effects("agent.test")
        )
        current = store.get(skill.entry_id)
        assert current.version == 2
        assert current.content.sop_text == "浓缩 SOP：核实订单后处理"
        assert current.memory_points == ["退款", "核实"]

    def test_fold_rejects_missing_entry(self) -> None:
        store, engine, config, buffer = _make_env()
        handler = make_handle_memory_fold(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(context=_ctx(), entry_id=str(uuid.uuid4()), content_text="x")
        assert not res.success
        assert "不在 store" in res.error

    def test_promote_creates_skill_with_task_provenance(self) -> None:
        """memory_promote 带 task_id → 结果 provenance（TaskResultRef）。"""
        store, engine, config, buffer = _make_env()
        handler = make_handle_memory_promote(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(
            context=_ctx(),
            title="退款处理技能",
            sop_text="先核实订单再退款",
            memory_points=["退款"],
            task_id="task.7",
            outcome="completed",
            note="三次退款两次成功",
        )
        assert res.success
        entry = store.get(uuid.UUID(res.data["entry_id"]))
        assert entry is not None
        assert entry.type == MemoryEntryType.SKILL
        assert entry.provenance.task_results == [
            TaskResultRef(task_id="task.7", outcome="completed", note="三次退款两次成功"),
        ]
        assert any(
            e.effect_type == EffectType.MEMORY_ENTRY_WRITE
            for e in buffer.get_effects("agent.test")
        )

    def test_promote_from_existing_entry(self) -> None:
        store, engine, config, buffer = _make_env()
        skill = _put_skill(store, engine, buffer)
        handler = make_handle_memory_promote(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(context=_ctx(), entry_id=str(skill.entry_id))
        assert res.success
        promoted = store.get(uuid.UUID(res.data["entry_id"]))
        assert promoted.entry_id != skill.entry_id  # 新条目（长期化）
        assert "退款处理" in promoted.content.sop_text

    def test_edit_appends_version(self) -> None:
        store, engine, config, buffer = _make_env()
        skill = _put_skill(store, engine, buffer)
        handler = make_handle_memory_edit(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(
            context=_ctx(),
            entry_id=str(skill.entry_id),
            content_text="更新后的 SOP",
        )
        assert res.success
        assert res.data["version"] == 2
        current = store.get(skill.entry_id)
        assert current.content.sop_text == "更新后的 SOP"
        # 版本链保留 v1（不可变版本链）
        assert len(store.get_chain(skill.entry_id)) == 2

    def test_retag_updates_triggers_and_index(self) -> None:
        store, engine, config, buffer = _make_env()
        skill = _put_skill(store, engine, buffer, memory_points=["旧词"])
        handler = make_handle_memory_retag(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(
            context=_ctx(),
            entry_id=str(skill.entry_id),
            memory_points=["新词", "退款"],
        )
        assert res.success
        current = store.get(skill.entry_id)
        assert current.memory_points == ["新词", "退款"]
        # 触发器索引同步（旧触发器移除、新触发器进入召回面）
        triggers = engine.index.entry_triggers(skill.entry_id)
        assert "旧词" not in triggers
        assert "新词" in triggers

    def test_evict_removes_entry_and_effect(self) -> None:
        store, engine, config, buffer = _make_env()
        skill = _put_skill(store, engine, buffer)
        handler = make_handle_memory_evict(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(context=_ctx(), entry_id=str(skill.entry_id))
        assert res.success
        assert store.get(skill.entry_id) is None
        assert any(
            e.effect_type == EffectType.MEMORY_ENTRY_EVICT
            for e in buffer.get_effects("agent.test")
        )
        # 索引同步移除
        assert engine.index.entry_triggers(skill.entry_id) == frozenset()

    def test_evict_missing_returns_error(self) -> None:
        store, engine, config, buffer = _make_env()
        handler = make_handle_memory_evict(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(context=_ctx(), entry_id=str(uuid.uuid4()))
        assert not res.success

    def test_pin_adds_persistent_query_terms(self) -> None:
        """memory_pin：条目标题/触发器并入可控查询词（防召回降级）。"""
        store, engine, config, buffer = _make_env()
        skill = _put_skill(store, engine, buffer, title="退款流程", memory_points=["退款"])
        handler = make_handle_memory_pin(
            {"agent.test": store}, {"agent.test": engine}, {"agent.test": config}, buffer,
        )
        res = handler(context=_ctx(), entry_id=str(skill.entry_id))
        assert res.success
        assert "退款流程" in config.persistent_query_terms
        assert "退款" in config.persistent_query_terms
        # MEMORY_PIN effect（INVERT_CONTRACT 已注册，可回滚）
        pin_effects = [
            e for e in buffer.get_effects("agent.test")
            if e.effect_type == EffectType.MEMORY_PIN
        ]
        assert len(pin_effects) == 1
        assert EffectType.MEMORY_PIN in INVERT_CONTRACT
        RecallEngine.rollback_pin_effect(config, pin_effects[0])
        assert config.persistent_query_terms == []

    def test_pin_is_idempotent_per_terms(self) -> None:
        store, engine, config, buffer = _make_env()
        skill = _put_skill(store, engine, buffer, title="退款流程")
        handler = make_handle_memory_pin(
            {"agent.test": store}, {"agent.test": engine}, {"agent.test": config}, buffer,
        )
        handler(context=_ctx(), entry_id=str(skill.entry_id))
        handler(context=_ctx(), entry_id=str(skill.entry_id))
        # 去重：不重复并入
        assert config.persistent_query_terms.count("退款流程") == 1

    def test_tools_reject_injected_entries(self) -> None:
        """外加载条目不入 store ⇒ 结构性不可改写（handler 拒绝）。"""
        store, engine, config, buffer = _make_env()
        handler = make_handle_memory_edit(
            {"agent.test": store}, {"agent.test": engine}, buffer,
        )
        res = handler(
            context=_ctx(),
            entry_id=str(uuid.uuid4()),  # 不在 store（外加载 = 不在 store）
            content_text="x",
        )
        assert not res.success
        assert "不在 store" in res.error


# ===========================================================================
# 5. 工具面收窄（CONSOLIDATING 下授权集 = 记忆工具集）
# ===========================================================================


class TestToolSurfaceNarrowing:
    """CONSOLIDATING 相位下授权集切换为记忆工具集（渲染侧 + 执行侧）。"""

    def _make_registry(self, phase: ContinuationPhase | None) -> ToolRegistry:
        authority = Authority(team_id=new_team_id(), owner_agent_id="agent.test")
        reg = ToolRegistry(
            authority=authority,
            phase_provider=lambda aid: phase,
        )
        for manifest in builtin_manifests().values():
            reg.register_manifest(manifest)
        reg.declare_tools(
            "agent.test",
            frozenset({"read", "write", "ls", *MEMORY_TOOL_NAMES}),
        )
        return reg

    def test_consolidating_narrows_authorized_tools(self) -> None:
        reg = self._make_registry(ContinuationPhase.CONSOLIDATING)
        authorized = reg.authorized_tools("agent.test")
        assert authorized == MEMORY_TOOL_NAMES  # 只留记忆工具集

    def test_normal_mode_keeps_granted_set(self) -> None:
        reg = self._make_registry(ContinuationPhase.READY_TO_DECIDE)
        authorized = reg.authorized_tools("agent.test")
        assert "read" in authorized
        assert "memory_fold" in authorized

    def test_consolidating_denies_non_memory_execution(self) -> None:
        reg = self._make_registry(ContinuationPhase.CONSOLIDATING)
        res = reg.execute(ToolContext(agent_id="agent.test", tick=1), "read", path="f.txt")
        assert not res.success
        assert res.error_code == "permission_denied"
        assert "CONSOLIDATING" not in res.error  # 走标准权限拒绝

    def test_consolidating_allows_memory_tools(self) -> None:
        """执行侧：CONSOLIDATING 下记忆工具可用（真实 handler 接线）。"""
        authority = Authority(team_id=new_team_id(), owner_agent_id="agent.test")
        reg = ToolRegistry(
            authority=authority,
            phase_provider=lambda aid: ContinuationPhase.CONSOLIDATING,
        )
        for manifest in builtin_manifests().values():
            reg.register_manifest(manifest)
        reg.declare_tools(
            "agent.test",
            frozenset({*MEMORY_TOOL_NAMES}),
        )
        store, engine, config, buffer = _make_env()
        reg.register_handler(
            "memory_retag",
            make_handle_memory_retag(
                {"agent.test": store}, {"agent.test": engine}, buffer,
            ),
        )
        skill = _put_skill(store, engine, buffer, memory_points=["旧"])
        res = reg.execute(
            ToolContext(agent_id="agent.test", tick=1),
            "memory_retag",
            entry_id=str(skill.entry_id),
            memory_points=["新"],
        )
        assert res.success
        assert store.get(skill.entry_id).memory_points == ["新"]


# ===========================================================================
# 6. 结构化摘要条目（provenance 记整理来源）
# ===========================================================================


class TestSummaryEntry:
    """结构化摘要 → MemoryEntry（type=skill，provenance 记整理来源）。"""

    def test_summary_entry_contains_required_fields(self) -> None:
        summary = ConsolidationSummary.model_validate(_consolidation_summary_dict())
        entry = make_summary_entry(summary, agent_id="agent.test", tick=7)
        assert entry.type == MemoryEntryType.SKILL
        content = entry.content.sop_text
        assert "反思与进步" in content
        assert "经验教训" in content
        assert "流程优化" in content
        assert "记忆链接" in content
        # 链接同时作为触发器（可被链接词召回）
        assert "退款流程" in entry.memory_points
        assert "退款流程" in entry.content.applies_to

    def test_summary_entry_provenance_records_consolidation_origin(self) -> None:
        summary = ConsolidationSummary.model_validate(_consolidation_summary_dict())
        entry = make_summary_entry(summary, agent_id="agent.test", tick=3)
        assert entry.provenance.consolidation_origin == "consolidating:agent.test:3"
        assert entry.provenance.origin.value == "own"

    def test_write_summary_entry_stages_effect_and_syncs_index(self) -> None:
        store, engine, config, buffer = _make_env()
        summary = ConsolidationSummary.model_validate(_consolidation_summary_dict())
        entry = write_summary_entry(
            store, engine, summary,
            agent_id="agent.test", tick=5, buffer=buffer,
        )
        assert store.get(entry.entry_id) is not None
        assert any(
            e.effect_type == EffectType.MEMORY_ENTRY_WRITE
            for e in buffer.get_effects("agent.test")
        )
        # 索引同步：链接词可召回摘要条目
        hits = engine.recall(
            store=store,
            config=config,
            contextual_terms=["退款流程"],
        )
        assert any(c.entry.entry_id == entry.entry_id for c in hits)


# ===========================================================================
# 7. LLMAgent：CONSOLIDATING 输出解析 → 动作 + 摘要 + 自决退出
# ===========================================================================


class TestLLMAgentConsolidating:
    """LLMAgent 在 CONSOLIDATING 会话中解析整理输出（确定性输入）。"""

    def _make_agent(self, reg: ToolRegistry) -> LLMAgent:
        gw = LLMGateway()
        gw.register_profile("test", LLMProviderConfig(provider="openai", model="gpt-4o"))
        gw.bind_agent("agent.test", "test")
        return LLMAgent(
            agent_id="agent.test",
            llm_gateway=gw,
            llm_profile="test",
            tool_registry=reg,
        )

    def test_consolidating_response_emits_exit_and_tool_intents(self) -> None:
        reg = ToolRegistry()
        reg.declare_tools("agent.test", frozenset({"memory_fold"}))
        reg.register_tool(
            builtin_manifests()["memory_fold"],
            make_handle_memory_fold({}, {}, TransactionBuffer()),
        )
        agent = self._make_agent(reg)

        cont = AgentContinuation(agent_id="agent.test")
        cont.enter_consolidating(tick=0)  # resume_phase=FRESH（会话标记）
        response = {
            "content": json.dumps({
                "consolidation_summary": _consolidation_summary_dict(),
                "exit": True,
            }),
            "tool_calls": [
                {
                    "id": "call.1",
                    "type": "function",
                    "function": {
                        "name": "memory_fold",
                        "arguments": json.dumps({
                            "entry_id": "00000000-0000-4000-8000-000000000001",
                            "content_text": "浓缩",
                        }),
                    },
                },
            ],
        }
        cont.receive_llm_result(response, tick=1)  # → PROCESSING_RESULT

        intents = agent.decide_intents(
            AgentObservation(agent_id="agent.test", tick=1),
            continuation=cont,
        )
        types = [i.intent_type for i in intents]
        # 整理动作（memory_fold）→ SubmitToolRequest
        assert IntentType.SUBMIT_TOOL_REQUEST in types
        tool_intent = [i for i in intents if isinstance(i, SubmitToolRequest)][0]
        assert tool_intent.tool_name == "memory_fold"
        # 结构化摘要 + 自决退出 → MemoryConsolidateIntent(exit)
        exit_intents = [i for i in intents if isinstance(i, MemoryConsolidateIntent)]
        assert len(exit_intents) == 1
        assert exit_intents[0].action == "exit"
        assert exit_intents[0].structured_summary["reflection_and_growth"] == "学会了先核实再处理"

    def test_consolidating_request_narrows_tools(self) -> None:
        """CONSOLIDATING 下 LLM 请求只见记忆工具定义（工具面收窄）。"""
        authority = Authority(team_id=new_team_id(), owner_agent_id="agent.test")
        reg = ToolRegistry(
            authority=authority,
            phase_provider=lambda aid: ContinuationPhase.CONSOLIDATING,
        )
        for manifest in builtin_manifests().values():
            reg.register_manifest(manifest)
        reg.declare_tools(
            "agent.test",
            frozenset({"read", "write", "memory_fold"}),
        )
        agent = self._make_agent(reg)
        cont = AgentContinuation(agent_id="agent.test")
        cont.enter_consolidating(tick=0)
        intents = agent.decide_intents(
            AgentObservation(agent_id="agent.test", tick=1),
            continuation=cont,
        )
        assert len(intents) == 1
        req = intents[0]
        assert req.intent_type == IntentType.SUBMIT_LLM_REQUEST
        tool_names = {t["name"] for t in req.tools}
        assert tool_names == {"memory_fold"}  # read/write 被收窄
        # 系统提示含整理指令（输出契约）
        system_content = "".join(
            m.get("content", "") for m in req.messages if m.get("role") == "system"
        )
        assert "CONSOLIDATING" in system_content
        assert "memory_links" in system_content

    def test_normal_response_with_request_marker_emits_enter(self) -> None:
        """普通模式下内容含主动整理请求标记 → MemoryConsolidateIntent(enter)。"""
        reg = ToolRegistry()
        reg.declare_tools("agent.test", frozenset({"read"}))
        agent = self._make_agent(reg)
        cont = AgentContinuation(agent_id="agent.test")
        cont.receive_llm_result(
            {"content": "我申请 memory_consolidate 整理记忆", "tool_calls": []},
            tick=1,
        )
        intents = agent.decide_intents(
            AgentObservation(agent_id="agent.test", tick=1),
            continuation=cont,
        )
        enters = [i for i in intents if isinstance(i, MemoryConsolidateIntent)]
        assert len(enters) == 1
        assert enters[0].action == "enter"


# ===========================================================================
# 8. Simulation 接线（触发 / 退出 / Journal）
# ===========================================================================


class TestConsolidationSimulation:
    """Simulation 写路径：预算触发进入、主动 intent 进入/退出、Journal 审计。"""

    def _inject_fixed_overflow(self, sim: Simulation) -> None:
        """固定注入需求超预算（ratio ≥ 0.9）→ pending_consolidation 路径。"""
        sim._context_compiler._fixed_memory_tokens = 10
        auth = sim._authority
        pos_id = auth.register_entity(kind=EntityKind.DATA, label="pos")
        eid = auth.register_entity(
            kind=EntityKind.DATA,
            label="jd",
            injection=InjectionDecl(
                content="x" * 200,  # 50 token > 10 budget → ratio 5.0
                source_tag="[POSITION_JD]",
            ),
        )
        auth.accept_device(auth)
        auth.grant_membership("agent.root", pos_id)
        auth.grant_capability(pos_id, eid, priority=1)

    def test_budget_trigger_enters_consolidating(self) -> None:
        """预算触发：Observe 只读标志/使用率 → decide 写路径置 CONSOLIDATING。"""
        sim = _make_sim()
        self._inject_fixed_overflow(sim)
        sim.run_tick()
        cont = sim._agent_runtime_states["agent.root"].continuation
        assert cont.phase == ContinuationPhase.CONSOLIDATING
        assert cont.resume_phase is not None

    def test_no_budget_no_consolidation(self) -> None:
        sim = _make_sim()
        sim.run_tick()
        cont = sim._agent_runtime_states["agent.root"].continuation
        assert cont.phase != ContinuationPhase.CONSOLIDATING

    def _act(self, sim: Simulation, tick: int, intent) -> list[ActionResult]:
        """直接驱动 _phase_act（写路径单测，绕过 decide 的 rule-based 意图）。"""
        action = AgentAction(
            action_type=intent.intent_type.value,
            tool_name=getattr(intent, "tool_name", ""),
            payload=dict(intent.payload),
        )
        validated = {"agent.root": [ActionResult(action=action, success=True)]}
        return sim._phase_act(
            tick,
            {"agent.root": [intent]},
            ready=[],
            validated=validated,
            snapshot={},
        )

    def test_active_enter_intent_sets_phase(self) -> None:
        """主动触发：MemoryConsolidateIntent(enter) → CONSOLIDATING + resume_phase。"""
        sim = _make_sim()
        cont = sim._agent_runtime_states["agent.root"].continuation
        cont.phase = ContinuationPhase.READY_TO_DECIDE
        intent = MemoryConsolidateIntent(agent_id="agent.root", action="enter")
        self._act(sim, 0, intent)
        assert cont.phase == ContinuationPhase.CONSOLIDATING
        assert cont.resume_phase == ContinuationPhase.READY_TO_DECIDE

    def test_exit_intent_restores_resume_phase_and_writes_summary(self) -> None:
        """自决退出：恢复 resume_phase + 结构化摘要写入（provenance 记整理来源）。"""
        sim = _make_sim()
        cont = sim._agent_runtime_states["agent.root"].continuation
        cont.advance_to_waiting_tool("req.9", tick=0)  # 被打断的工作
        cont.enter_consolidating(tick=0)
        assert cont.phase == ContinuationPhase.CONSOLIDATING

        intent = MemoryConsolidateIntent(
            agent_id="agent.root",
            action="exit",
            structured_summary=_consolidation_summary_dict(),
        )
        self._act(sim, 0, intent)

        # 退出 → 恢复 WAITING_FOR_TOOL（被打断的工作立即续上）
        assert cont.phase == ContinuationPhase.WAITING_FOR_TOOL
        assert cont.resume_phase is None

        # 摘要条目写入 store（provenance 记整理来源）
        entries = sim._agent_memories["agent.root"].list_entries()
        summaries = [e for e in entries if e.provenance.consolidation_origin]
        assert len(summaries) == 1
        assert summaries[0].provenance.consolidation_origin == "consolidating:agent.root:0"
        content = summaries[0].content.sop_text
        assert "反思与进步" in content
        assert "经验教训" in content
        assert "流程优化" in content
        assert "记忆链接" in content

    def test_exit_without_consolidating_is_noop(self) -> None:
        sim = _make_sim()
        cont = sim._agent_runtime_states["agent.root"].continuation
        intent = MemoryConsolidateIntent(agent_id="agent.root", action="exit")
        self._act(sim, 0, intent)
        assert cont.phase != ContinuationPhase.CONSOLIDATING

    def test_journal_records_memory_effects(self) -> None:
        """动作入 Journal：整理动作（含摘要写入）的 effect 可审计。"""
        sim = _make_sim()
        cont = sim._agent_runtime_states["agent.root"].continuation
        cont.enter_consolidating(tick=0)
        intent = MemoryConsolidateIntent(
            agent_id="agent.root",
            action="exit",
            structured_summary=_consolidation_summary_dict(),
        )
        sim._journal.start_tick(0, 0)
        results = self._act(sim, 0, intent)
        assert results["agent.root"][0].success
        sim._phase_commit(0, results)
        record = sim._journal.for_tick(0)
        assert record is not None
        effect_types = {e.effect_type for e in record.effects}
        # 摘要写入 = MEMORY_ENTRY_WRITE（Journal 可审计，可回滚）
        assert "memory_entry_write" in effect_types
        assert any(
            e.agent_id == "agent.root" and "memory:" in e.resource
            for e in record.effects
        )

    def test_exit_intent_works_after_result_finalize(self) -> None:
        """回归：处理完整理响应后 phase 已回落 READY_TO_DECIDE（finalize），
        act 中的 exit 仍须按会话标记（resume_phase）恢复被打断的工作。"""
        sim = _make_sim()
        cont = sim._agent_runtime_states["agent.root"].continuation
        cont.advance_to_waiting_tool("req.9", tick=0)   # 被打断的工作
        cont.enter_consolidating(tick=0)                 # resume_phase=WAITING_FOR_TOOL
        cont.receive_llm_result({"content": "", "tool_calls": []}, tick=1)
        cont.finalize_result_processing(tick=1)          # → READY_TO_DECIDE（resume_phase 仍置位）

        intent = MemoryConsolidateIntent(
            agent_id="agent.root",
            action="exit",
            structured_summary=_consolidation_summary_dict(),
        )
        self._act(sim, 1, intent)

        assert cont.phase == ContinuationPhase.WAITING_FOR_TOOL
        assert cont.resume_phase is None
        # 摘要已写入
        entries = sim._agent_memories["agent.root"].list_entries()
        assert any(e.provenance.consolidation_origin for e in entries)

    def test_session_wide_tool_narrowing(self) -> None:
        """工具面收窄覆盖整个整理会话：处理完整理响应（phase 已回落
        READY_TO_DECIDE）后，非记忆工具仍被拒绝、渲染侧仍只留记忆工具。"""
        sim = _make_sim()
        cont = sim._agent_runtime_states["agent.root"].continuation
        cont.enter_consolidating(tick=0)
        cont.receive_llm_result({"content": "", "tool_calls": []}, tick=1)
        cont.finalize_result_processing(tick=1)  # READY_TO_DECIDE，会话仍在
        assert cont.resume_phase is not None
        # 执行侧：read 属基础授予集，但会话期内被收窄拒绝
        res = sim._tool_registry.execute(
            ToolContext(agent_id="agent.root", tick=1), "read", path="f.txt",
        )
        assert not res.success
        assert res.error_code == "permission_denied"
        # 渲染侧：授权集 = 记忆工具集
        assert sim._tool_registry.authorized_tools("agent.root") == MEMORY_TOOL_NAMES

    def test_budget_exit_threshold(self) -> None:
        """预算回落阈值下退出（hysteresis 出 80%）：decide 写路径恢复相位。"""
        sim = _make_sim()
        cont = sim._agent_runtime_states["agent.root"].continuation
        cont.phase = ContinuationPhase.WAITING_FOR_TOOL
        cont.enter_consolidating(tick=0)  # resume_phase=WAITING_FOR_TOOL
        # 模拟 observe 后 usage_ratio 回落（< 0.8）：直接驱动 gate
        gate = sim._consolidation_gate
        assert gate.should_exit(usage_ratio=0.79) is True
        cont.exit_consolidating(tick=1)
        assert cont.phase == ContinuationPhase.WAITING_FOR_TOOL
        assert cont.resume_phase is None

    def test_memory_tools_registered_and_granted(self) -> None:
        """记忆工具集已注册并授予（CONSOLIDATING 可用前提）。"""
        sim = _make_sim()
        manifests = {m.name for m in sim._tool_registry.manifests()}
        assert MEMORY_TOOL_NAMES <= manifests
        authorized = sim._tool_registry.authorized_tools("agent.root")
        assert MEMORY_TOOL_NAMES <= authorized
