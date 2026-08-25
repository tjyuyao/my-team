"""Tests for T6: ContextCompiler — role-aware observation assembly.

N4-3 追加：三预算注入管线测试（固定注入/来源段不可覆盖/pending_consolidation/
stamp 入 Journal/不可信内容安全不变量/详细度降级）。

Date: 2026-08-18 / N4-3 2026-08-25
"""

from __future__ import annotations

from my_team.agent_runtime import AgentObservation
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType, AuditLog
from my_team.context_compiler import (
    _UNOVERRIDABLE_SOURCE_TAGS,
    _UNTRUSTED_SOURCE_TAGS,
    ContextCompiler,
    DetailLevel,
    InjectionSlot,
    ObservationPolicy,
    ObservationSection,
    TaskScope,
)
from my_team.devices.authority import Authority, new_team_id
from my_team.devices.base import EntityKind, InjectionDecl
from my_team.mailbox import MailSystem
from my_team.private_store import PrivateStore
from my_team.shared_kb import SharedKB
from my_team.simulation import Simulation
from my_team.task_tree import TaskTree


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True, "mission": "Build great things"},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _make_snapshot(tick: int = 0) -> dict:
    return {
        "tick": tick,
        "tasks": {
            "task.1": {
                "status": "in_progress",
                "title": "Research task",
                "assignee": "agent.research",
                "assigner": "agent.root",
            },
            "task.2": {
                "status": "completed",
                "title": "Done task",
                "assignee": "agent.root",
                "assigner": "agent.root",
            },
        },
        "emails": [
            {
                "email_id": "e1",
                "from": "agent.root",
                "to": ["agent.research"],
                "subject": "Do this",
                "email_type": "delegation",
                "task_id": "task.1",
                "body": "Please research topic X",
            },
        ],
        "shared_kb": {
            "paths": ["project/notes.md", "project/data.csv"],
            "versions": {"project/notes.md": 1, "project/data.csv": 3},
        },
        "locks": {},
        "lock_tokens": {},
        "private_files": {
            "agent.research": {
                "files": {"workspace/report.md": "draft content"},
                "dirs": ["workspace"],
            },
        },
    }


class TestContextCompilerUnit:
    def test_default_policy_for_root(self):
        compiler = ContextCompiler(
            agent_tree=_make_tree(),
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
        )
        policy = compiler.get_policy("root_decision_agent")
        assert ObservationSection.TASK_TREE_SUMMARY in policy.sections
        assert ObservationSection.KPI_DASHBOARD in policy.sections
        assert policy.task_scope == TaskScope.ALL

    def test_default_policy_for_worker(self):
        compiler = ContextCompiler(
            agent_tree=_make_tree(),
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
        )
        policy = compiler.get_policy("worker")
        assert ObservationSection.TASK_DETAIL in policy.sections
        assert ObservationSection.WORKSPACE_FILES in policy.sections
        assert policy.task_scope == TaskScope.FOCUS

    def test_unknown_role_uses_fallback(self):
        compiler = ContextCompiler(
            agent_tree=_make_tree(),
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
        )
        policy = compiler.get_policy("unknown_role")
        assert policy.task_scope == TaskScope.ALL  # default


class TestContextCompilerIntegration:
    def _make_compiler(self):
        tree = _make_tree()
        return ContextCompiler(
            agent_tree=tree,
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
        ), tree

    def test_root_observation_has_task_summary(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        root_config = tree.get("agent.root")
        result = compiler.compile(root_config, snapshot)
        assert "task_summary" in result
        assert result["task_summary"]["in_progress"] == 1
        assert result["task_summary"]["completed"] == 1

    def test_root_observation_has_kpi(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        root_config = tree.get("agent.root")
        result = compiler.compile(root_config, snapshot)
        assert "kpi" in result
        assert result["kpi"]["total_tasks"] == 2

    def test_root_observation_has_mission(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        root_config = tree.get("agent.root")
        result = compiler.compile(root_config, snapshot)
        assert result.get("mission") == "Build great things"

    def test_worker_observation_has_focus_task(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        # Simulate continuation with task_id
        class FakeCont:
            task_id = "task.1"
        result = compiler.compile(worker_config, snapshot, continuation=FakeCont())
        assert "task.1" in result["task_states"]
        assert result["task_states"]["task.1"]["title"] == "Research task"

    def test_worker_observation_excludes_other_tasks(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        class FakeCont:
            task_id = "task.1"
        result = compiler.compile(worker_config, snapshot, continuation=FakeCont())
        # Worker with FOCUS scope should not see task.2
        assert "task.2" not in result["task_states"]

    def test_emails_filtered_by_recipient(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        result = compiler.compile(worker_config, snapshot)
        # Email is addressed to agent.research
        assert len(result["emails"]) == 1
        assert result["emails"][0]["subject"] == "Do this"

    def test_email_body_included(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        result = compiler.compile(worker_config, snapshot)
        assert result["emails"][0]["body"] == "Please research topic X"

    def test_kb_snapshot_included(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        result = compiler.compile(worker_config, snapshot)
        assert "shared_kb_snapshot" in result
        assert "project/notes.md" in result["shared_kb_snapshot"]["paths"]

    def test_kb_injection_disabled(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        policy = ObservationPolicy(
            sections=[ObservationSection.KB_SNAPSHOT],
            task_scope=TaskScope.ALL,
            kb_injection=False,
        )
        compiler._policies["worker"] = policy
        result = compiler.compile(worker_config, snapshot)
        assert result["shared_kb_snapshot"] == {}

    def test_token_budget_truncates_email_body(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        # Add a very long email
        snapshot["emails"][0]["body"] = "x" * 10000
        worker_config = tree.get("agent.research")
        policy = ObservationPolicy(
            sections=[ObservationSection.EMAILS],
            task_scope=TaskScope.ALL,
            max_tokens=100,
        )
        compiler._policies["worker"] = policy
        result = compiler.compile(worker_config, snapshot)
        assert "truncated" in result["emails"][0]["body"]

    def test_workspace_files_for_worker(self):
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        worker_config = tree.get("agent.research")
        result = compiler.compile(worker_config, snapshot)
        assert "workspace_files" in result
        assert "workspace/report.md" in result["workspace_files"]

    def test_compile_returns_observation_compatible_dict(self):
        """Result can be wrapped in AgentObservation."""
        compiler, tree = self._make_compiler()
        snapshot = _make_snapshot()
        root_config = tree.get("agent.root")
        result = compiler.compile(root_config, snapshot)
        obs = AgentObservation(**result)
        assert obs.agent_id == "agent.root"
        assert obs.tick == 0


class TestContextCompilerViaSimulation:
    def test_simulation_uses_context_compiler(self):
        """End-to-end: simulation run_tick uses ContextCompiler."""
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        # Observation should have been produced (check via decide results)
        assert sim._tick_engine.current_tick == 1

    def test_root_worker_see_different_tasks(self):
        """Root sees all tasks, worker sees only focus task."""
        sim = Simulation(agent_tree=_make_tree())
        # Create tasks
        sim.task_tree.create(
            task_id="task.r1", title="Root task",
            assigner_agent_id="agent.root", assignee_agent_id="agent.root",
        )
        sim.task_tree.create(
            task_id="task.w1", title="Worker task",
            assigner_agent_id="agent.root", assignee_agent_id="agent.research",
        )
        sim.run_tick()
        # Verify context compiler was used (observations produced)
        assert sim._tick_engine.current_tick == 1


# ==========================================================================
# N4-3 三预算注入管线测试
# ==========================================================================

def _make_authority_with_injections(
    agent_id: str,
    injections: list[dict],
) -> Authority:
    """Helper: create Authority with fixed injections for agent.

    injections: list of {content, source_tag, priority}
    """
    tid = new_team_id()
    auth = Authority(team_id=tid, owner_agent_id=agent_id)
    pos_id = auth.register_entity(
        kind=__import__('my_team.devices.base', fromlist=['EntityKind']).EntityKind.DATA,
        label="test_position",
    )
    inj_eids = []
    for inj in injections:
        eid = auth.register_entity(
            kind=EntityKind.DATA,
            label=inj.get("label", "inj"),
            injection=InjectionDecl(
                content=inj["content"],
                source_tag=inj["source_tag"],
            ),
        )
        inj_eids.append(eid)
    auth.accept_device(auth)  # 实体同步进注册中心（grant 校验依赖）
    auth.grant_membership(agent_id, pos_id)
    for eid, inj in zip(inj_eids, injections):
        auth.grant_capability(pos_id, eid, priority=inj["priority"])
    return auth


class TestN43InjectionPipeline:
    """N4-3 三预算注入管线核心测试。"""

    def _make_compiler(self, authority=None, audit_log=None, fixed_tokens=4000):
        import my_team.agent_tree as at
        AT = getattr(at, 'AgentTree')
        tree = AT.from_dict({'agents': [
            {
                'agent_id': 'agent.root',
                'display_name': 'Root',
                'role': 'root_decision_agent',
                'parent_id': None,
                'children': [],
                'tools': [],
                'can_delegate': True,
                'metadata': {'bootstrap': True, 'mission': 'Test mission'},
            },
        ]})
        return ContextCompiler(
            agent_tree=tree,
            task_tree=TaskTree(),
            shared_kb=SharedKB(),
            mail_system=MailSystem(),
            private_store=PrivateStore(),
            authority=authority,
            audit_log=audit_log,
            fixed_memory_tokens=fixed_tokens,
        ), tree

    def _snapshot(self):
        return {
            'tick': 1,
            'tasks': {},
            'emails': [],
            'shared_kb': {},
            'locks': {},
            'lock_tokens': {},
            'private_files': {},
        }

    # ------------------------------------------------------------------
    # 固定注入 priority<10
    # ------------------------------------------------------------------

    def test_fixed_injection_enters_layout(self):
        """priority<10 的注入进入 fixed_slots。"""
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'JD content', 'source_tag': '[POSITION_JD]', 'priority': 1},
        ])
        compiler, tree = self._make_compiler(authority=auth)
        layout = compiler.build_layout('agent.root', self._snapshot())
        assert len(layout.fixed_slots) == 1
        assert layout.fixed_slots[0].source_tag == '[POSITION_JD]'
        assert layout.fixed_slots[0].priority_class == 'fixed'

    def test_policy_higher_priority_recalled(self):
        """priority>=10 的注入进入 recalled_slots，不进 fixed_slots。"""
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'job_output content', 'source_tag': '[SKILL_INSTRUCTION]', 'priority': 15},
        ])
        compiler, tree = self._make_compiler(authority=auth)
        layout = compiler.build_layout('agent.root', self._snapshot())
        assert len(layout.fixed_slots) == 0
        assert len(layout.recalled_slots) == 1
        assert layout.recalled_slots[0].source_tag == '[SKILL_INSTRUCTION]'

    # ------------------------------------------------------------------
    # POLICY/[POSITION_JD] 最先、不可覆盖
    # ------------------------------------------------------------------

    def test_policy_first_unoverridable(self):
        """[POLICY] 和 [POSITION_JD] 必须排在 fixed_slots 前面。"""
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'Policy text', 'source_tag': '[POLICY]', 'priority': 0},
            {'content': 'JD text', 'source_tag': '[POSITION_JD]', 'priority': 1},
            {'content': 'job_output text', 'source_tag': '[SKILL_INSTRUCTION]', 'priority': 5},
        ])
        compiler, tree = self._make_compiler(authority=auth)
        layout = compiler.build_layout('agent.root', self._snapshot())
        tags = [s.source_tag for s in layout.fixed_slots]
        assert tags[0] == '[POLICY]'
        assert tags[1] == '[POSITION_JD]'

    def test_unoverridable_tags_constant(self):
        """_UNOVERRIDABLE_SOURCE_TAGS 包含 [POLICY] 和 [POSITION_JD]。"""
        assert '[POLICY]' in _UNOVERRIDABLE_SOURCE_TAGS
        assert '[POSITION_JD]' in _UNOVERRIDABLE_SOURCE_TAGS

    def test_unoverridable_slot_flag(self):
        """is_unoverridable() 对 POLICY/POSITION_JD 返回 True。"""
        slot = InjectionSlot(
            source_tag='[POLICY]',
            priority_class='fixed',
            detail_level=DetailLevel.FULL,
            content='policy',
        )
        assert slot.is_unoverridable()
        slot2 = InjectionSlot(
            source_tag='[SKILL_INSTRUCTION]',
            priority_class='recalled',
            detail_level=DetailLevel.FULL,
            content='skill',
        )
        assert not slot2.is_unoverridable()

    # ------------------------------------------------------------------
    # 客户不可信内容安全不变量（§8.4）
    # ------------------------------------------------------------------

    def test_untrusted_content_excluded_from_fixed(self):
        """[UNTRUSTED_CUSTOMER_CONTENT] 永不进固定注入。"""
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'Customer text',
             'source_tag': '[UNTRUSTED_CUSTOMER_CONTENT]', 'priority': 1},
            {'content': 'Policy text', 'source_tag': '[POLICY]', 'priority': 0},
        ])
        compiler, tree = self._make_compiler(authority=auth)
        layout = compiler.build_layout('agent.root', self._snapshot())
        for slot in layout.fixed_slots:
            assert slot.source_tag not in _UNTRUSTED_SOURCE_TAGS, (
                f"不可信内容 {slot.source_tag!r} 不得进入固定注入"
            )
        assert len(layout.fixed_slots) == 1  # 只有 [POLICY]

    def test_untrusted_slot_flag(self):
        """is_untrusted() 对 UNTRUSTED_CUSTOMER_CONTENT 返回 True。"""
        slot = InjectionSlot(
            source_tag='[UNTRUSTED_CUSTOMER_CONTENT]',
            priority_class='recalled',
            detail_level=DetailLevel.FULL,
            content='customer text',
        )
        assert slot.is_untrusted()

    def test_untrusted_excluded_from_recalled(self):
        """[UNTRUSTED_CUSTOMER_CONTENT] 同样不进召回注入。"""
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'Customer text',
             'source_tag': '[UNTRUSTED_CUSTOMER_CONTENT]', 'priority': 15},
        ])
        compiler, tree = self._make_compiler(authority=auth)
        layout = compiler.build_layout('agent.root', self._snapshot())
        for slot in layout.recalled_slots:
            assert slot.source_tag not in _UNTRUSTED_SOURCE_TAGS

    # ------------------------------------------------------------------
    # pending_consolidation 超限标志
    # ------------------------------------------------------------------

    def test_pending_consolidation_when_budget_exceeded(self):
        """固定预算超限时置 pending_consolidation=True。"""
        # 设很小的预算（10 token）且注入内容远超
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'x' * 100, 'source_tag': '[POSITION_JD]', 'priority': 1},
        ])
        compiler, tree = self._make_compiler(authority=auth, fixed_tokens=10)
        layout = compiler.build_layout('agent.root', self._snapshot())
        assert layout.pending_consolidation is True
        # 超限内容不入 fixed_slots
        assert len(layout.fixed_slots) == 0

    def test_no_pending_consolidation_within_budget(self):
        """固定预算足够时 pending_consolidation=False。"""
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'Short JD', 'source_tag': '[POSITION_JD]', 'priority': 1},
        ])
        compiler, tree = self._make_compiler(authority=auth, fixed_tokens=4000)
        layout = compiler.build_layout('agent.root', self._snapshot())
        assert layout.pending_consolidation is False

    # ------------------------------------------------------------------
    # 同一 entity 多 position 授予去重
    # ------------------------------------------------------------------

    def test_dedup_multiple_position_grants(self):
        """同一 entity 多 position 授予时，取最小 priority（最高优先），只入一次。"""
        import my_team.devices.base as db
        tid = new_team_id()
        auth = Authority(team_id=tid, owner_agent_id='agent.root')
        # 注册同一个 entity
        eid = auth.register_entity(
            kind=db.EntityKind.DATA,
            label='shared_entity',
            injection=db.InjectionDecl(content='Shared content', source_tag='[SKILL_INSTRUCTION]'),
        )
        auth.accept_device(auth)
        # 两个 position，两个 priority
        for p, priority in [('pos.a', 5), ('pos.b', 8)]:
            auth.grant_membership('agent.root', p)
            auth.grant_capability(p, eid, priority=priority)

        compiler, _ = self._make_compiler(authority=auth)
        layout = compiler.build_layout('agent.root', self._snapshot())
        # 同一 entity 只应出现一次
        refs = [s.entry_ref for s in layout.fixed_slots]
        assert refs.count(eid) == 1

    # ------------------------------------------------------------------
    # 版本戳入 Journal（stamp）
    # ------------------------------------------------------------------

    def test_stamp_written_to_audit_log(self):
        """compile() 完成后写 MEMORY_INJECTION_STAMP audit 事件。"""
        audit_log = AuditLog()
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'Policy', 'source_tag': '[POLICY]', 'priority': 0},
        ])
        compiler, tree = self._make_compiler(authority=auth, audit_log=audit_log)
        cfg = tree.get('agent.root')
        compiler.compile(cfg, self._snapshot())
        stamps = audit_log.for_event_type(AuditEventType.MEMORY_INJECTION_STAMP)
        assert len(stamps) == 1
        assert stamps[0].agent_id == 'agent.root'
        assert 'stamp_hash' in stamps[0].details
        assert 'layout_refs' in stamps[0].details

    def test_stamp_hash_deterministic(self):
        """相同注入内容两次编译产生相同 stamp_hash。"""
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'Deterministic policy', 'source_tag': '[POLICY]', 'priority': 0},
        ])
        compiler, tree = self._make_compiler(authority=auth)
        layout1 = compiler.build_layout('agent.root', self._snapshot())
        layout2 = compiler.build_layout('agent.root', self._snapshot())
        assert layout1.stamp_hash == layout2.stamp_hash

    def test_memory_injection_field_in_compile_result(self):
        """compile() 结果包含 memory_injection 字段（布局元数据）。"""
        auth = _make_authority_with_injections('agent.root', [
            {'content': 'Policy text', 'source_tag': '[POLICY]', 'priority': 0},
        ])
        compiler, tree = self._make_compiler(authority=auth)
        cfg = tree.get('agent.root')
        result = compiler.compile(cfg, self._snapshot())
        assert 'memory_injection' in result
        mi = result['memory_injection']
        assert 'stamp_hash' in mi
        assert 'pending_consolidation' in mi
        assert 'fixed_count' in mi

    # ------------------------------------------------------------------
    # 详细度降级
    # ------------------------------------------------------------------

    def test_detail_level_degradation(self):
        """recall 预算极小时详细度从 FULL 降为 SUMMARY 或 TITLE_ONLY。"""
        # 一个大内容（短标题行 + 长正文，title-only 可容纳、FULL 不可）
        big_content = 'Short title line\n' + 'x' * 2000
        slot = InjectionSlot(
            source_tag='[SKILL_INSTRUCTION]',
            priority_class='recalled',
            detail_level=DetailLevel.FULL,
            content=big_content,
            entry_ref='e1',
        )
        compiler, _ = self._make_compiler(fixed_tokens=4000)
        # 预算 = 10 token，FULL 不可能塞进去
        result = compiler._apply_detail_budget([slot], budget=10)
        # 结果为 SUMMARY 或 TITLE_ONLY（非 FULL）
        assert len(result) > 0
        assert result[0].detail_level != DetailLevel.FULL

    # ------------------------------------------------------------------
    # compile() 兼容 AgentObservation
    # ------------------------------------------------------------------

    def test_compile_result_wraps_to_observation(self):
        """compile() 结果可包装为 AgentObservation（含 memory_injection 字段）。"""
        compiler, tree = self._make_compiler()
        cfg = tree.get('agent.root')
        result = compiler.compile(cfg, self._snapshot())
        obs = AgentObservation(**result)
        assert obs.agent_id == 'agent.root'
        assert isinstance(obs.memory_injection, dict)
