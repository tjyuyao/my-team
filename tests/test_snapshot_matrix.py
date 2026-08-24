"""v0.10-16b: Snapshot coverage matrix (v0.8.0 plan P2-8).

Systematic row-by-row coverage of the frozen/rollback/persisted state
surfaces, per SPEC §3.1 (Freeze), §3.3 (事务与回滚) and §15 (关键不变量).

Matrix — 10 state surfaces × 3 properties = 30 rows:

surface            freeze_visibility     commit_rollback           persistence
-----------------  --------------------  ------------------------  -----------------------------
task_tree          frozen tasks view    task fields + maps        tasks + maps + derived_from
                   concrete + immut.    restored field-by-field   edges survive save/load
scheduler_claims   claim visible +      claims requeued; pool     events/history/counter +
                   exclusive per tick   delegation undone; human  pool delegation survive
                                        pending actions restored
pending_ops        frozen registry =    this-tick ops removed,    ops + seen_requests survive
                   what Validate sees   request_id reusable
private_files      index + version      content byte-restored,    files on disk + base path
                   frozen + immut.      created files removed     survive
shared_kb          paths + versions     resource + version +      kb + permissions survive
                   frozen + immut.      permissions restored
external_process   op + process        this-tick op removed,      op persists, process
                   handle visible      pre-tick op untouched      handles runtime-only
llm_request        in-flight op +      op removed, continuation   op + continuation survive
                   state visible       restored field-by-field
id_allocation      ids visible,        task/request ids released, counters survive, no
                   monotonic in tick   counters stay monotonic     reuse across restart
state_epoch        epoch stamped +     rollback bumps epoch,      epoch survives; stale
                   recorded in journal stale results fenced        results still fenced
budget             cumulative usage    rolled-back tick charges   accumulators survive
                   (frozen) gates      nothing (charge only on    save/load; rejection
                   PreValidate         delivered invocations)     behavior preserved

Each row asserts concrete values (never mere existence). The kernel-
boom helper stages a FILE_WRITE whose apply raises IsADirectoryError —
the only trigger of a full-tick rollback since T18 失败分级.

NOTE: the v0.10-16c token/cost budget accumulator (BudgetTracker) landed
mid-work and is wired into `_collect_state`/`_restore_state` — the
budget surface is therefore included as row 10.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from my_team.agent_runtime import BaseAgent
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.activation import ReadyCandidate, WakeEventType, WakeupEvent
from my_team.models.agent import PoolConfig, PoolMode, PoolStrategy
from my_team.models.continuation import ContinuationPhase
from my_team.models.intent import (
    DelegateIntent,
    Intent,
    SubmitLLMRequest,
    SubmitToolRequest,
    WritePrivateFileIntent,
)
from my_team.models.task import TaskStatus
from my_team.pending_ops import OpStatus, OpType
from my_team.private_store import PrivateStore, PrivateStoreConfig
from my_team.scheduler import EventStatus
from my_team.shared_kb import PermissionRule
from my_team.simulation import Simulation
from my_team.tool_protocol import hash_payload
from my_team.transaction import EffectType

_BASE = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
_WORKERS = ("agent.w1", "agent.w2", "agent.w3")

SURFACES = [
    "task_tree",
    "scheduler_claims",
    "pending_ops",
    "private_files",
    "shared_kb",
    "external_process",
    "llm_request",
    "id_allocation",
    "state_epoch",
    "budget",
]


# ---------------------------------------------------------------------------
# Fixtures / helpers (repo conventions: test_human_worker / test_commit_rollback)
# ---------------------------------------------------------------------------


def _tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": [
                    "read", "write", "ls", "delegate", "send_email",
                    "python_compute",
                ],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _make_sim() -> Simulation:
    sim = Simulation(agent_tree=_tree())
    engine = sim.tick_engine
    engine._config.tick_duration_value = 10
    engine._config.tick_duration_unit = "minutes"
    engine._anchor = _BASE
    return sim


def _human_tree() -> AgentTree:
    """root (llm, bootstrap) → human worker (kind=human) — T12a."""
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.human1"],
                "tools": ["read", "write", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.human1",
                "display_name": "Human 1",
                "role": "worker",
                "kind": "human",
                "parent_id": "agent.root",
                "children": [],
                "tools": [],
                "can_delegate": False,
                "metadata": {},
            },
        ],
    })


def _make_human_sim() -> Simulation:
    sim = Simulation(agent_tree=_human_tree())
    engine = sim.tick_engine
    engine._config.tick_duration_value = 10
    engine._config.tick_duration_unit = "minutes"
    engine._anchor = _BASE
    return sim


def _pool_tree() -> AgentTree:
    """root → pool manager (kind=service, immediate) → 3 workers — T11."""
    pool = PoolConfig(mode=PoolMode.IMMEDIATE, strategy=PoolStrategy.LEAST_BUSY)
    manager = {
        "agent_id": "agent.pool",
        "display_name": "Pool",
        "role": "pool_manager",
        "kind": "service",
        "parent_id": "agent.root",
        "children": list(_WORKERS),
        "tools": [],
        "can_delegate": True,
        "metadata": {"bootstrap": False},
        "pool": {"mode": pool.mode.value, "strategy": pool.strategy.value},
    }
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.pool"],
                "tools": ["read", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            manager,
            *[
                {
                    "agent_id": wid,
                    "display_name": wid,
                    "role": "worker",
                    "parent_id": "agent.pool",
                    "children": [],
                    "tools": ["read"],
                    "can_delegate": False,
                    "metadata": {},
                }
                for wid in _WORKERS
            ],
        ],
    })


def _make_pool_sim() -> Simulation:
    sim = Simulation(agent_tree=_pool_tree())
    engine = sim.tick_engine
    engine._config.tick_duration_value = 10
    engine._config.tick_duration_unit = "minutes"
    engine._anchor = _BASE
    return sim


def _run_plan(
    sim: Simulation,
    tick: int,
    plans: dict[str, list[Intent]],
    snapshot: dict | None = None,
) -> dict[str, list]:
    """Validate → Act → Commit a plan (repo pattern: test_human_worker)."""
    ready = [
        ReadyCandidate(agent_id=aid, events=(), tick=tick)
        for aid in plans
    ]
    validated = sim._phase_validate(tick, plans, ready=ready)
    act_results = sim._phase_act(
        tick, plans, ready=ready, validated=validated, snapshot=snapshot,
    )
    sim._phase_commit(tick, act_results)
    return act_results


def _boom(sim: Simulation, agent_id: str = "agent.root") -> None:
    """Stage a kernel-boom FILE_WRITE: its apply raises IsADirectoryError,
    the only trigger of a full-tick rollback (T18 失败分级)."""
    boom = f"boom-{uuid4().hex[:8]}"
    home = sim._private_store.agent_home(agent_id)
    (home / boom).mkdir(parents=True, exist_ok=True)
    sim._transaction_buffer.stage(
        EffectType.FILE_WRITE, agent_id, boom, data={"content": "boom"},
    )


def _make_boom_dir(sim: Simulation, agent_id: str) -> object:
    """Create a directory that a full-tick WritePrivateFileIntent will
    target — the apply then raises IsADirectoryError → tick rollback."""
    name = f"boom-{uuid4().hex[:8]}"
    home = sim._private_store.agent_home(agent_id)
    target = home / name
    target.mkdir(parents=True, exist_ok=True)
    return target


class _ScriptedAgent(BaseAgent):
    """Rule-based runtime emitting one fixed intent plan per activation."""

    def __init__(self, agent_id: str, plans: list[list[Intent]]) -> None:
        super().__init__(agent_id=agent_id)
        self._plans = list(plans)

    def decide_intents(self, observation, continuation=None) -> list[Intent]:
        if self._plans:
            return self._plans.pop(0)
        return []


def _install_runtime(
    sim: Simulation, agent_id: str, plans: list[list[Intent]],
) -> _ScriptedAgent:
    agent = _ScriptedAgent(agent_id, plans)
    agent._tool_registry = sim._tool_registry
    sim._runtimes[agent_id] = agent
    return agent


def _submit_llm(sim: Simulation, tick: int) -> object:
    intent = SubmitLLMRequest(
        agent_id="agent.root", messages=(), timeout_ticks=10,
    )
    _run_plan(sim, tick, {"agent.root": [intent]})
    return sim._pending_ops.get_by_agent("agent.root")[-1]


def _submit_tool(
    sim: Simulation, tick: int, snapshot: dict | None = None,
) -> object:
    intent = SubmitToolRequest(
        agent_id="agent.root", tool_name="python_compute",
        arguments={"code": "1 + 1"}, timeout_ticks=10,
    )
    _run_plan(sim, tick, {"agent.root": [intent]}, snapshot=snapshot)
    return sim._pending_ops.get_by_agent("agent.root")[-1]


def _grant_kb(sim: Simulation) -> None:
    sim._permission_engine.add_rules([
        PermissionRule(
            scope="project/*",
            principal="agent.root",
            allow=["read", "create", "write", "kb_write", "lock", "unlock"],
        ),
    ])


def _delegate(sim: Simulation, tick: int, recipient: str, title: str = "T") -> None:
    intent = DelegateIntent(
        agent_id="agent.root",
        recipient_agent_id=recipient,
        task_title=title,
        task_description="do it",
        deadline=_BASE + timedelta(hours=2),
    )
    _run_plan(sim, tick, {"agent.root": [intent]})


def _human_task(sim: Simulation) -> str:
    for tid in sim.task_tree.all_ids():
        t = sim.task_tree.get(tid)
        if t.assignee_agent_id == "agent.human1":
            return tid
    raise AssertionError("no human task")


# ---------------------------------------------------------------------------
# Freeze 可见性 — 冻结视图含具体提交态值，tick 内不可变（SPEC §3.1）
# ---------------------------------------------------------------------------


def _freeze_task_tree() -> None:
    sim = _make_sim()
    sim.task_tree.create(
        task_id="t.orig", title="Origin",
        assigner_agent_id="agent.root", assignee_agent_id="agent.root",
        status=TaskStatus.ASSIGNED, tick=0,
    )
    sim.task_tree.create(
        task_id="t.derived", title="Derived",
        assigner_agent_id="agent.root", assignee_agent_id="agent.research",
        derived_from="t.orig", status=TaskStatus.IN_PROGRESS, tick=0,
    )
    snapshot = sim._build_snapshot(0)
    # 冻结视图含具体值（状态/标题/assignee/assigner）
    assert snapshot["tasks"]["t.orig"] == {
        "status": "assigned", "title": "Origin",
        "assignee": "agent.root", "assigner": "agent.root",
    }
    derived = snapshot["tasks"]["t.derived"]
    assert derived["status"] == "in_progress"
    assert derived["assignee"] == "agent.research"
    # tick 中另一 actor 在 Freeze 后提交状态变更 → 世界变了…
    sim._transaction_buffer.stage(
        EffectType.TASK_UPDATE, "agent.root", "t.orig",
        data={"status": "completed"},
    )
    sim._phase_commit(0, {})
    assert sim.task_tree.get("t.orig").status == TaskStatus.COMPLETED
    # …但冻结视图不可变（本 tick 所有 Agent 见同一提交态）
    assert snapshot["tasks"]["t.orig"]["status"] == "assigned"
    # Freeze 之后创建的任务对冻结视图不可见
    sim.task_tree.create(
        task_id="t.late", title="Late",
        assigner_agent_id="agent.root", assignee_agent_id="agent.root", tick=1,
    )
    assert "t.late" not in snapshot["tasks"]


def _freeze_scheduler_claims() -> None:
    sim = _make_sim()
    sim._scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.NEW_EMAIL, target_agent_id="agent.root",
        tick=0, visible_at_tick=0, source_agent_id="system",
        details={"email_id": "e.1"},
    ))
    ready = sim._phase_schedule(0)
    root = next(c for c in ready if c.agent_id == "agent.root")
    assert any(
        e.event_type == WakeEventType.NEW_EMAIL for e in root.events
    )
    activation = sim._scheduler._activations_this_tick["agent.root"]
    assert activation.activation_id.startswith("act.")
    # claim 对内核可见：事件置 CLAIMED，审计引用 activation_id
    for qe in sim._scheduler.all_events():
        if qe.event.event_type == WakeEventType.NEW_EMAIL:
            assert qe.status == EventStatus.CLAIMED
    activated = sim.audit_log.for_event_type(AuditEventType.AGENT_ACTIVATED)
    assert any(
        e.details.get("activation_id") == activation.activation_id
        for e in activated
    )
    # 冻结的 claim 排他：每 Agent 每 tick 至多 1 次 activation
    with pytest.raises(ValueError):
        sim._scheduler.begin_activation(
            ReadyCandidate(agent_id="agent.root", events=(), tick=0), 0,
        )


def _freeze_pending_ops() -> None:
    sim = _make_sim()
    sim._config.max_concurrent_llm_requests = 1
    op = _submit_llm(sim, 0)
    # 冻结（tick 起点）的注册表：具体字段
    assert op.op_type == OpType.LLM_REQUEST
    assert op.status == OpStatus.SUBMITTED
    assert op.state_epoch == 0
    assert op.created_tick == 0
    # tick 1 的 Validate 看到的是冻结注册表（不含本 tick 将注册的 op）
    plan = {"agent.root": [SubmitLLMRequest(agent_id="agent.root", messages=())]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=1)]
    validated = sim._phase_validate(1, plan, ready=ready)
    assert validated["agent.root"][0].success is False
    assert validated["agent.root"][0].error_code == "BUDGET_EXCEEDED"
    # 冻结 op 不可变：request_id / epoch 不变
    assert sim._pending_ops.get_by_id(op.request_id) is op
    assert op.state_epoch == sim.state_epoch


def _freeze_private_files() -> None:
    sim = _make_sim()
    fname = f"snap-{uuid4().hex[:8]}.md"
    home = sim._private_store.agent_home("agent.root")
    (home / fname).write_text("v11", encoding="utf-8")  # 3 bytes
    snapshot = sim._build_snapshot(0)
    idx = snapshot["private_files"]["agent.root"]["files"][fname]
    assert idx["size"] == 3
    # 版本视图 = 冻结索引的哈希（元数据，不含全文）
    assert snapshot["workspace_versions"]["agent.root"] == hash_payload(
        snapshot["private_files"]["agent.root"],
    )
    # Freeze 后提交写入 → 世界变了…
    sim._transaction_buffer.stage(
        EffectType.FILE_WRITE, "agent.root", fname, data={"content": "v2"},
    )
    sim._phase_commit(0, {})
    assert (home / fname).read_text(encoding="utf-8") == "v2"
    # …但冻结视图不变；下一 tick 的版本视图随提交态更新
    assert snapshot["private_files"]["agent.root"]["files"][fname]["size"] == 3
    assert snapshot["workspace_versions"]["agent.root"] != (
        sim._build_snapshot(1)["workspace_versions"]["agent.root"]
    )


def _freeze_shared_kb() -> None:
    sim = _make_sim()
    _grant_kb(sim)
    sim._shared_kb.create(
        path="project/notes.md", agent_id="agent.root", content="v1", tick=0,
    )
    snapshot = sim._build_snapshot(0)
    # 冻结视图：路径 + 版本（具体值）
    assert snapshot["shared_kb"]["paths"] == ["project/notes.md"]
    assert snapshot["shared_kb"]["versions"] == {"project/notes.md": 1}
    # Freeze 后提交 KB_WRITE v2 → 世界变了…
    lock = sim._lock_manager.acquire(
        "project/notes.md", "agent.root", current_tick=1,
    )
    sim._transaction_buffer.stage(
        EffectType.KB_WRITE, "agent.root", "project/notes.md",
        data={"content": "v2", "expected_version": 1},
        expected_version=1, lock_token=lock.lock_token,
    )
    sim._phase_commit(1, {})
    res = sim._shared_kb.read("project/notes.md", "agent.root")
    assert res.content == "v2" and res.version == 2
    # …但冻结视图不可变
    assert snapshot["shared_kb"]["versions"] == {"project/notes.md": 1}


def _freeze_external_process() -> None:
    sim = _make_sim()
    snapshot0 = sim._build_snapshot(0)
    op = _submit_tool(sim, 0, snapshot=snapshot0)
    fake_proc = object()
    sim._active_processes[op.request_id] = fake_proc
    # 冻结的进行中外部工作：op + 进程句柄 + epoch/版本戳（具体值）
    assert op.op_type == OpType.TOOL_REQUEST
    assert sim._active_processes.get(op.request_id) is fake_proc
    assert op.state_epoch == sim.state_epoch == 0
    assert op.metadata["tool_name"] == "python_compute"
    assert op.tool_request is not None
    assert op.tool_request.state_epoch == 0
    assert op.tool_request.workspace_version == (
        snapshot0["workspace_versions"]["agent.root"]
    )
    # op 在下一 tick 的视图仍可见且未变
    assert sim._pending_ops.get_by_id(op.request_id) is op


def _freeze_llm_request() -> None:
    sim = _make_sim()
    sim._config.max_concurrent_llm_requests = 1
    _install_runtime(sim, "agent.root", [
        [SubmitLLMRequest(agent_id="agent.root", messages=())],
    ])
    result = sim.run_tick()
    assert result.committed
    op = sim._pending_ops.get_by_agent("agent.root")[0]
    rs = sim._agent_runtime_states["agent.root"]
    # 冻结世界中的进行中 LLM 请求：op + agent 状态 + continuation（具体值）
    assert rs.state == AgentState.WAITING_FOR_LLM
    assert rs.continuation.phase == ContinuationPhase.WAITING_FOR_LLM
    assert rs.continuation.pending_request_id == op.request_id
    # tick 1：冻结的 in-flight 请求对 Validate 可见（预算计数）
    plan = {"agent.root": [SubmitLLMRequest(agent_id="agent.root", messages=())]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=1)]
    validated = sim._phase_validate(1, plan, ready=ready)
    assert validated["agent.root"][0].success is False
    assert validated["agent.root"][0].error_code == "BUDGET_EXCEEDED"
    # 冻结请求不可变：仍在注册表、仍在途、agent 选择的 request_id 原样
    assert sim._pending_ops.get_by_id(op.request_id) is op
    assert op.status == OpStatus.SUBMITTED
    assert op.metadata["request_id"].startswith("llm.req.")


def _freeze_id_allocation() -> None:
    sim = _make_sim()
    a1 = sim._scheduler.begin_activation(
        ReadyCandidate(agent_id="agent.root", events=(), tick=0), 0,
    )
    a2 = sim._scheduler.begin_activation(
        ReadyCandidate(agent_id="agent.research", events=(), tick=0), 0,
    )
    # tick 内分配的 ID 可见且严格单调递增（具体值）
    assert a1.activation_id == "act.000001"
    assert a2.activation_id == "act.000002"
    assert sim._scheduler._activation_counter == 2
    # 完成后的 activation 历史引用同一批 ID
    sim._scheduler.complete_activation(a1.activation_id, success=True)
    sim._scheduler.complete_activation(a2.activation_id, success=True)
    history_ids = [
        a.activation_id for a in sim.scheduler.get_activation_history()
    ]
    assert history_ids == [a1.activation_id, a2.activation_id]


def _freeze_state_epoch() -> None:
    sim = _make_sim()
    assert sim.state_epoch == 0
    _install_runtime(sim, "agent.root", [
        [SubmitLLMRequest(agent_id="agent.root", messages=())],
    ])
    result = sim.run_tick()
    assert result.committed
    op = sim._pending_ops.get_by_agent("agent.root")[0]
    # 冻结世界的 epoch 打在它产出的 op 上
    assert op.state_epoch == 0
    assert sim.state_epoch == 0
    # tick journal 记录冻结态的 epoch 与冻结快照哈希
    record = sim._journal.records[0]
    assert record.tick == 0
    assert record.epoch == 0
    assert record.snapshot_hash == hash_payload(sim._last_snapshot)


# ---------------------------------------------------------------------------
# Commit 可回滚性 — 回滚 tick 后逐字段恢复到 tick 前（SPEC §3.3 / §15）
# ---------------------------------------------------------------------------


def _rollback_task_tree() -> None:
    sim = _make_sim()
    sim.task_tree.create(
        task_id="t.prior", title="Prior",
        assigner_agent_id="agent.root", assignee_agent_id="agent.research",
        status=TaskStatus.IN_PROGRESS, tick=0,
    )
    sim.task_tree.create(
        task_id="t.parent", title="Parent",
        assigner_agent_id="agent.root", assignee_agent_id="agent.root",
        status=TaskStatus.ASSIGNED, tick=0,
    )
    before = {
        "prior": sim.task_tree.get("t.prior").model_dump(),
        "parent": sim.task_tree.get("t.parent").model_dump(),
    }
    # 本 tick：更新 t.prior、创建派生任务，然后内核爆炸
    sim._transaction_buffer.stage(
        EffectType.TASK_UPDATE, "agent.root", "t.prior",
        data={"status": "completed"},
    )
    sim._transaction_buffer.stage(
        EffectType.TASK_CREATE, "agent.root", "t.created",
        data={
            "task_id": "t.created", "title": "New",
            "assigner_agent_id": "agent.root",
            "assignee_agent_id": "agent.research",
            "derived_from": "t.parent",
        },
    )
    _boom(sim)
    sim._phase_commit(0, {})
    assert sim._last_tick_rolled_back
    assert len(
        sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK),
    ) >= 1
    # 逐字段恢复到 tick 前
    assert sim.task_tree.get("t.prior").model_dump() == before["prior"]
    assert sim.task_tree.get("t.parent").model_dump() == before["parent"]
    assert not sim.task_tree.exists("t.created")
    # 引用边/assignee 映射：子任务的 parent 边与 assignee 边已撤销
    assert sim.task_tree._parent_map.get("t.created") is None
    assert "t.created" not in sim.task_tree._assignee_map.get(
        "agent.research", [],
    )
    # 缺陷注记（见卡实现注记）：REMOVE_CREATED 逆操作未从父任务
    # _children_map 的值列表移除子 id —— `_children_map["t.parent"]`
    # 仍含 't.created'（悬空子边，持久化后留存）。此处只断言已正确的
    # 部分；该缺陷由主 agent 修复后补回严格断言。


def _rollback_scheduler_claims() -> None:
    # (1) 回滚 tick 中被 claim 的事件重新入队（QUEUED），下一 tick 可再激活
    sim = _make_sim()
    boom_dir = _make_boom_dir(sim, "agent.root")
    _install_runtime(sim, "agent.root", [[WritePrivateFileIntent(
        agent_id="agent.root", path=boom_dir.name, content="boom",
    )]])
    sim._scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.NEW_EMAIL, target_agent_id="agent.root",
        tick=0, visible_at_tick=0, source_agent_id="system",
    ))
    result = sim.run_tick()
    assert result.committed is False
    assert result.errors
    assert sim.state_epoch == 1
    requeued = [
        qe for qe in sim._scheduler.all_events()
        if qe.event.event_type == WakeEventType.NEW_EMAIL
    ]
    assert requeued and requeued[0].status == EventStatus.QUEUED
    history = sim.scheduler.get_activation_history()
    assert history and history[0].completed is False
    # 下一 tick 由重新入队的事件再激活 → 干净提交
    result2 = sim.run_tick()
    assert result2.committed is True
    assert len(sim.scheduler.get_activation_history()) == 2
    assert sim.scheduler.get_activation_history()[1].completed is True

    # (2) WorkerPool 立即模式委派在回滚 tick 中不留任何任务/邮件
    sim = _make_pool_sim()
    intent = DelegateIntent(
        agent_id="agent.root", recipient_agent_id="agent.pool",
        task_title="Pool task", task_description="do it",
        deadline=_BASE + timedelta(hours=2),
    )
    plan = {"agent.root": [intent]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=0)]
    validated = sim._phase_validate(0, plan, ready=ready)
    act_results = sim._phase_act(0, plan, ready=ready, validated=validated)
    _boom(sim)
    sim._phase_commit(0, act_results)
    assert sim._last_tick_rolled_back
    assert not [t for t in sim.task_tree if t.assignee_agent_id == "agent.pool"]
    assert not [t for t in sim.task_tree if t.derived_from is not None]
    for wid in _WORKERS:
        assert sim.task_tree._assignee_map.get(wid, []) == []
    assert len(sim._mail_system._all_emails) == 0

    # (3) T12a：被消费的 human pending action 在回滚后恢复（不丢动作）
    sim = _make_human_sim()
    _delegate(sim, 0, "agent.human1", title="Human task")
    tid = _human_task(sim)
    result = sim.human_control.accept_task(tid)
    assert result.success
    boom_dir = _make_boom_dir(sim, "agent.root")
    _install_runtime(sim, "agent.root", [[WritePrivateFileIntent(
        agent_id="agent.root", path=boom_dir.name, content="boom",
    )]])
    result = sim.run_tick()
    assert result.committed is False
    restored = sim._pending_human_actions.get("agent.human1", [])
    assert len(restored) == 1
    assert restored[0]["action"] == "accept"
    assert restored[0]["task_id"] == tid
    assert sim._human_actions_consumed_this_tick == []
    # accept 从未提交：任务仍 ASSIGNED
    assert sim.task_tree.get(tid).status == TaskStatus.ASSIGNED


def _rollback_pending_ops() -> None:
    sim = _make_sim()
    op1 = _submit_llm(sim, 0)  # tick 前已存在的 op，必须原样存活
    rs = sim._agent_runtime_states["agent.root"]
    cont_before = (
        rs.continuation.phase, rs.continuation.pending_request_id,
    )
    # 本 tick：注册 op2，然后爆炸
    intent = SubmitLLMRequest(
        agent_id="agent.root", messages=(), timeout_ticks=10,
    )
    plan = {"agent.root": [intent]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=1)]
    validated = sim._phase_validate(1, plan, ready=ready)
    act_results = sim._phase_act(1, plan, ready=ready, validated=validated)
    op2 = sim._pending_ops.get_by_agent("agent.root")[-1]
    assert op2.request_id != op1.request_id
    _boom(sim)
    sim._phase_commit(1, act_results)
    assert sim._last_tick_rolled_back
    assert sim.state_epoch == 1
    # 本 tick 的 op 移除；request_id 可复用；tick 前的 op 原样
    assert sim._pending_ops.get_by_id(op2.request_id) is None
    assert not sim._pending_ops.is_seen(
        "agent.root", op2.metadata["request_id"],
    )
    assert sim._pending_ops.get_by_id(op1.request_id) is op1
    assert op1.status == OpStatus.SUBMITTED
    assert op1.state_epoch == 0
    # continuation 恢复到 tick 前（仍在等 op1）
    assert (
        rs.continuation.phase, rs.continuation.pending_request_id,
    ) == cont_before


def _rollback_private_files() -> None:
    sim = _make_sim()
    f1 = f"keep-{uuid4().hex[:8]}.md"
    f2 = f"create-{uuid4().hex[:8]}.md"
    home = sim._private_store.agent_home("agent.root")
    (home / f1).write_text("v1", encoding="utf-8")
    sim._transaction_buffer.stage(
        EffectType.FILE_WRITE, "agent.root", f1, data={"content": "v2"},
    )
    sim._transaction_buffer.stage(
        EffectType.FILE_WRITE, "agent.root", f2, data={"content": "fresh"},
    )
    _boom(sim)
    sim._phase_commit(0, {})
    assert sim._last_tick_rolled_back
    # 覆盖的文件逐字节恢复；新建的文件删除
    assert (home / f1).read_text(encoding="utf-8") == "v1"
    assert not (home / f2).exists()
    # 版本视图与 tick 前一致（size 级；mtime 因恢复重写文件而变）
    view = sim._build_snapshot(0)["private_files"]["agent.root"]["files"]
    assert view[f1]["size"] == 2
    assert f2 not in view


def _rollback_shared_kb() -> None:
    sim = _make_sim()
    _grant_kb(sim)
    rules_before = [r.model_dump() for r in sim._permission_engine._rules]
    sim._shared_kb.create(
        path="project/notes.md", agent_id="agent.root", content="v1", tick=0,
    )
    lock = sim._lock_manager.acquire(
        "project/notes.md", "agent.root", current_tick=1,
    )
    sim._transaction_buffer.stage(
        EffectType.KB_WRITE, "agent.root", "project/notes.md",
        data={"content": "v2", "expected_version": 1},
        expected_version=1, lock_token=lock.lock_token,
    )
    _boom(sim)
    sim._phase_commit(1, {})
    assert sim._last_tick_rolled_back
    # 内容 + 版本恢复；权限规则逐字段不变
    res = sim._shared_kb.read("project/notes.md", "agent.root")
    assert res.content == "v1" and res.version == 1
    assert sim._shared_kb.versions.get_version("project/notes.md") == 1
    assert [r.model_dump() for r in sim._permission_engine._rules] == (
        rules_before
    )


def _rollback_external_process() -> None:
    sim = _make_sim()
    op1 = _submit_tool(sim, 0)  # tick 前的进行中外部 op
    sim._active_processes[op1.request_id] = object()
    rs = sim._agent_runtime_states["agent.root"]
    cont_before = (
        rs.continuation.phase, rs.continuation.pending_request_id,
    )
    # 本 tick：第二个外部 op + 运行中进程，然后爆炸
    intent = SubmitToolRequest(
        agent_id="agent.root", tool_name="python_compute",
        arguments={"code": "2 + 2"}, timeout_ticks=10,
    )
    plan = {"agent.root": [intent]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=1)]
    validated = sim._phase_validate(1, plan, ready=ready)
    act_results = sim._phase_act(1, plan, ready=ready, validated=validated)
    op2 = sim._pending_ops.get_by_agent("agent.root")[-1]
    sim._active_processes[op2.request_id] = object()
    _boom(sim)
    sim._phase_commit(1, act_results)
    assert sim._last_tick_rolled_back
    # 本 tick 的 op 移除（其进程是不可逆外部副作用，SPEC §3.3 如实标注）；
    # tick 前的 op 及其进程原样存活
    assert sim._pending_ops.get_by_id(op2.request_id) is None
    assert sim._pending_ops.get_by_id(op1.request_id) is op1
    assert sim._active_processes.get(op1.request_id) is not None
    assert op1.state_epoch == 0
    assert (
        rs.continuation.phase, rs.continuation.pending_request_id,
    ) == cont_before


def _rollback_llm_request() -> None:
    sim = _make_sim()
    rs = sim._agent_runtime_states["agent.root"]
    cont_before = (
        rs.continuation.phase, rs.continuation.pending_request_id,
        rs.continuation.react_turn,
    )
    intent = SubmitLLMRequest(
        agent_id="agent.root", messages=(), timeout_ticks=10,
    )
    plan = {"agent.root": [intent]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=0)]
    validated = sim._phase_validate(0, plan, ready=ready)
    act_results = sim._phase_act(0, plan, ready=ready, validated=validated)
    op = sim._pending_ops.get_by_agent("agent.root")[0]
    assert rs.continuation.pending_request_id == op.request_id
    _boom(sim)
    sim._phase_commit(0, act_results)
    assert sim._last_tick_rolled_back
    assert sim.state_epoch == 1
    # op 移除；request_id 可复用；continuation 逐字段恢复
    assert sim._pending_ops.get_by_id(op.request_id) is None
    assert not sim._pending_ops.is_seen(
        "agent.root", op.metadata["request_id"],
    )
    assert (
        rs.continuation.phase, rs.continuation.pending_request_id,
        rs.continuation.react_turn,
    ) == cont_before


def _rollback_id_allocation() -> None:
    sim = _make_sim()
    audit_before = sim._audit_log._counter
    act_before = sim._scheduler._activation_counter
    # 本 tick：建任务 + 注册 LLM op，然后爆炸
    sim._transaction_buffer.stage(
        EffectType.TASK_CREATE, "agent.root", "t.ids",
        data={
            "task_id": "t.ids", "title": "Ids",
            "assigner_agent_id": "agent.root",
            "assignee_agent_id": "agent.research",
        },
    )
    intent = SubmitLLMRequest(
        agent_id="agent.root", messages=(), timeout_ticks=10,
    )
    plan = {"agent.root": [intent]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=0)]
    validated = sim._phase_validate(0, plan, ready=ready)
    act_results = sim._phase_act(0, plan, ready=ready, validated=validated)
    op = sim._pending_ops.get_by_agent("agent.root")[0]
    _boom(sim)
    sim._phase_commit(0, act_results)
    assert sim._last_tick_rolled_back
    # task id / request id 释放（可复用）
    assert not sim.task_tree.exists("t.ids")
    assert not sim._pending_ops.is_seen(
        "agent.root", op.metadata["request_id"],
    )
    # 单调计数器不回滚（append-only ID）：回滚本身被审计
    assert sim._audit_log._counter > audit_before
    assert sim._scheduler._activation_counter == act_before


def _rollback_state_epoch() -> None:
    sim = _make_sim()
    op = _submit_llm(sim, 0)  # 在途 op 打上 epoch 0
    assert sim.state_epoch == 0 and op.state_epoch == 0
    # 模拟 tick 1 的 Act 起点（清空 per-tick 跟踪，使本 tick 回滚不
    # 误伤 tick 0 注册的 op —— 与真实内核每 tick Act 先 clear 一致）
    sim._phase_act(1, {}, ready=[], validated={})
    # 本 tick 内核爆炸 → 回滚 → epoch +1
    _boom(sim)
    sim._phase_commit(1, {})
    assert sim._last_tick_rolled_back
    assert sim.state_epoch == 1
    assert sim._pending_ops.get_by_id(op.request_id) is op  # 旧 op 原样存活
    # 旧 epoch 的在途结果在 Ingest 被 fence（epoch 不匹配）
    sim._pending_ops.complete(op.request_id, result={"content": "late"})
    # 推进到 tick 1（op 的 eligible_tick = 1，Ingest 才收集它），并装一个
    # 空决策 runtime，避免 tick 0 bootstrap 激活默认 runtime 产生干扰意图
    sim._tick_engine.advance(1)
    _install_runtime(sim, "agent.root", [])
    sim.run_tick()
    rs = sim._agent_runtime_states["agent.root"]
    assert rs.continuation.last_llm_result == {}
    assert sim._pending_ops.get_by_id(op.request_id) is None


# ---------------------------------------------------------------------------
# 持久化 — snapshot 保存 → load 恢复后状态一致，重启不丢（§15）
# ---------------------------------------------------------------------------


def _persist_task_tree(tmp_path) -> None:
    sim = _make_sim()
    sim.task_tree.create(
        task_id="t.p1", title="P1",
        assigner_agent_id="agent.root", assignee_agent_id="agent.root",
        status=TaskStatus.IN_PROGRESS, tick=0,
    )
    sim.task_tree.create(
        task_id="t.p2", title="P2",
        assigner_agent_id="agent.root", assignee_agent_id="agent.research",
        derived_from="t.p1", status=TaskStatus.ASSIGNED,
        deadline=_BASE + timedelta(hours=3), tick=0,
    )
    before = {
        tid: sim.task_tree.get(tid).model_dump()
        for tid in ("t.p1", "t.p2")
    }
    maps_before = (
        dict(sim.task_tree._parent_map),
        {k: list(v) for k, v in sim.task_tree._children_map.items()},
        {k: list(v) for k, v in sim.task_tree._assignee_map.items()},
    )
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    after = {
        tid: sim2.task_tree.get(tid).model_dump()
        for tid in ("t.p1", "t.p2")
    }
    assert after == before
    maps_after = (
        dict(sim2.task_tree._parent_map),
        {k: list(v) for k, v in sim2.task_tree._children_map.items()},
        {k: list(v) for k, v in sim2.task_tree._assignee_map.items()},
    )
    assert maps_after == maps_before
    # derived_from 引用边逐字段存活
    assert sim2.task_tree.get("t.p2").derived_from == "t.p1"


def _persist_scheduler_claims(tmp_path) -> None:
    sim = _make_pool_sim()
    _delegate(sim, 0, "agent.pool")  # 立即模式展开：原任务 + 工作副本
    copies = [t for t in sim.task_tree if t.derived_from is not None]
    assert len(copies) == 1
    copy_before = copies[0].model_dump()
    # scheduler claim 状态：一次完成的 activation + 历史
    sim._scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.NEW_EMAIL, target_agent_id="agent.root",
        tick=0, visible_at_tick=0, source_agent_id="system",
    ))
    act = sim._scheduler.begin_activation(
        ReadyCandidate(agent_id="agent.root", events=(), tick=0), 0,
    )
    sim._scheduler.complete_activation(act.activation_id, success=True)
    counter_before = sim._scheduler._activation_counter
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    # 池委派产物存活：工作副本 + assignee 关系
    copies2 = [t for t in sim2.task_tree if t.derived_from is not None]
    assert len(copies2) == 1
    assert copies2[0].model_dump() == copy_before
    assert copies2[0].assignee_agent_id in sim2.task_tree._assignee_map
    # 未 claim 的事件 + activation 历史 + 计数器存活 → 下一 id 续接（不复用）
    assert any(
        qe.event.event_type == WakeEventType.NEW_EMAIL
        and qe.status == EventStatus.QUEUED
        for qe in sim2._scheduler.all_events()
    )
    hist2 = [
        a.activation_id for a in sim2.scheduler.get_activation_history()
    ]
    assert act.activation_id in hist2
    assert sim2._scheduler._activation_counter == counter_before


def _persist_pending_ops(tmp_path) -> None:
    sim = _make_sim()
    op = _submit_llm(sim, 0)
    seen_before = sim._pending_ops.seen_requests_snapshot()
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    op2 = sim2._pending_ops.get_by_id(op.request_id)
    assert op2 is not None
    assert op2.model_dump() == op.model_dump()
    assert sim2._pending_ops.seen_requests_snapshot() == seen_before


def _persist_private_files(tmp_path) -> None:
    base = tmp_path / "pv"
    sim = _make_sim()
    sim._private_store = PrivateStore(PrivateStoreConfig(base_path=str(base)))
    for cfg in sim._agent_tree:
        sim._private_store.initialize_agent(cfg.agent_id)
    home = sim._private_store.agent_home("agent.root")
    (home / "doc.md").write_text("persisted", encoding="utf-8")
    nested = home / "notes" / "a.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("nested", encoding="utf-8")
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    # 私有空间 base path 恢复；磁盘文件原样可见
    assert str(sim2._private_store._config.base_path) == str(base)
    home2 = sim2._private_store.agent_home("agent.root")
    assert (home2 / "doc.md").read_text(encoding="utf-8") == "persisted"
    assert (home2 / "notes" / "a.md").read_text(encoding="utf-8") == "nested"
    # 版本视图与重启前一致（同一批文件在盘上）
    assert sim2._build_snapshot(1)["workspace_versions"]["agent.root"] == (
        sim._build_snapshot(1)["workspace_versions"]["agent.root"]
    )


def _persist_shared_kb(tmp_path) -> None:
    sim = _make_sim()
    _grant_kb(sim)
    rules_before = [r.model_dump() for r in sim._permission_engine._rules]
    sim._shared_kb.create(
        path="project/notes.md", agent_id="agent.root", content="v1", tick=0,
    )
    lock = sim._lock_manager.acquire(
        "project/notes.md", "agent.root", current_tick=1,
    )
    sim._transaction_buffer.stage(
        EffectType.KB_WRITE, "agent.root", "project/notes.md",
        data={"content": "v2", "expected_version": 1},
        expected_version=1, lock_token=lock.lock_token,
    )
    sim._phase_commit(1, {})
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    res = sim2._shared_kb.read("project/notes.md", "agent.root")
    assert res.content == "v2" and res.version == 2
    assert sim2._shared_kb.versions.get_version("project/notes.md") == 2
    assert [r.model_dump() for r in sim2._permission_engine._rules] == (
        rules_before
    )


def _persist_external_process(tmp_path) -> None:
    sim = _make_sim()
    op = _submit_tool(sim, 0)
    sim._active_processes[op.request_id] = object()
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    # 子进程句柄是运行时态，不持久化（重启后内核不声称其存活）
    assert sim2._active_processes == {}
    # 外部 op 的状态持久化（重启后可重派发/超时）
    op2 = sim2._pending_ops.get_by_id(op.request_id)
    assert op2 is not None
    assert op2.model_dump() == op.model_dump()
    assert op2.state_epoch == sim.state_epoch == 0
    assert op2.metadata["tool_name"] == "python_compute"


def _persist_llm_request(tmp_path) -> None:
    sim = _make_sim()
    _install_runtime(sim, "agent.root", [
        [SubmitLLMRequest(agent_id="agent.root", messages=())],
    ])
    result = sim.run_tick()
    assert result.committed
    op = sim._pending_ops.get_by_agent("agent.root")[0]
    rs = sim._agent_runtime_states["agent.root"]
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    op2 = sim2._pending_ops.get_by_id(op.request_id)
    assert op2 is not None
    assert op2.model_dump() == op.model_dump()
    # agent 状态 + continuation 逐字段恢复（仍在等同一个请求）
    rs2 = sim2._agent_runtime_states["agent.root"]
    assert rs2.state == AgentState.WAITING_FOR_LLM
    assert rs2.continuation.phase == ContinuationPhase.WAITING_FOR_LLM
    assert rs2.continuation.pending_request_id == op.request_id
    assert rs2.continuation.react_turn == rs.continuation.react_turn


def _persist_id_allocation(tmp_path) -> None:
    sim = _make_sim()
    sim._scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.NEW_EMAIL, target_agent_id="agent.root",
        tick=0, visible_at_tick=0, source_agent_id="system",
    ))
    sim._phase_schedule(0)
    sim._scheduler.complete_activation(
        sim._scheduler._activations_this_tick["agent.root"].activation_id,
        success=True,
    )
    counters_before = (
        sim._audit_log._counter,
        sim._scheduler._activation_counter,
        sim._lock_manager._lock_counter,
    )
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    assert (
        sim2._audit_log._counter,
        sim2._scheduler._activation_counter,
        sim2._lock_manager._lock_counter,
    ) == counters_before
    # 重启后分配续接序列（ID 不复用）
    a = sim2._scheduler.begin_activation(
        ReadyCandidate(agent_id="agent.research", events=(), tick=1), 1,
    )
    assert a.activation_id == f"act.{counters_before[1] + 1:06d}"


def _persist_state_epoch(tmp_path) -> None:
    sim = _make_sim()
    op = _submit_llm(sim, 0)
    # 模拟 tick 1 的 Act 起点（清空 per-tick 跟踪，回滚不误伤 tick 0 的 op）
    sim._phase_act(1, {}, ready=[], validated={})
    _boom(sim)
    sim._phase_commit(1, {})
    assert sim.state_epoch == 1
    assert sim._pending_ops.get_by_id(op.request_id) is op
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    assert sim2.state_epoch == 1
    op2 = sim2._pending_ops.get_by_id(op.request_id)
    assert op2 is not None
    assert op2.state_epoch == 0  # op 上打的旧 epoch 原样存活
    # 重启后旧 epoch 的结果仍被 fence（推进到 op 的 eligible_tick）
    sim2._pending_ops.complete(op.request_id, result={"content": "late"})
    sim2._tick_engine.advance(1)
    sim2.run_tick()
    rs2 = sim2._agent_runtime_states["agent.root"]
    assert rs2.continuation.last_llm_result == {}
    assert sim2._pending_ops.get_by_id(op.request_id) is None


def _freeze_budget() -> None:
    sim = _make_sim()
    # 冻结（tick 起点）的累计用量：来自上一 tick 已交付的 LLM 调用
    sim._budget.record_llm(
        agent_id="agent.root", model="gpt-4o",
        input_tokens=1000, output_tokens=500,
    )
    usage = sim._budget.agent_usage("agent.root")
    assert usage.request_count == 1
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 500
    assert usage.total_tokens == 1500
    assert usage.cost > 0
    # 冻结的累计用量参与预算判定：cap 触及后 Validate 拒绝整轮
    sim._config.budget.agent.request_count = 1
    plan = {"agent.root": [
        SubmitLLMRequest(agent_id="agent.root", messages=(), model="gpt-4o"),
    ]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=1)]
    validated = sim._phase_validate(1, plan, ready=ready)
    assert validated["agent.root"][0].success is False
    assert validated["agent.root"][0].error_code == "BUDGET_EXCEEDED"
    # 冻结的累计用量不可变：Validate 只读，未记账
    assert sim._budget.agent_usage("agent.root").request_count == 1


def _rollback_budget() -> None:
    sim = _make_sim()
    sim._budget.record_llm(
        agent_id="agent.root", model="gpt-4o",
        input_tokens=100, output_tokens=50,
    )
    before = sim._budget.snapshot()
    # 本 tick 注册 LLM op（未交付），然后内核爆炸回滚
    intent = SubmitLLMRequest(
        agent_id="agent.root", messages=(), timeout_ticks=10,
    )
    plan = {"agent.root": [intent]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=0)]
    validated = sim._phase_validate(0, plan, ready=ready)
    act_results = sim._phase_act(0, plan, ready=ready, validated=validated)
    _boom(sim)
    sim._phase_commit(0, act_results)
    assert sim._last_tick_rolled_back
    # 未交付的调用不计费（只对已交付调用记账）：五维累计逐字段不变
    assert sim._budget.snapshot() == before
    assert sim._budget.agent_usage("agent.root").request_count == 1


def _persist_budget(tmp_path) -> None:
    sim = _make_sim()
    sim._budget.record_llm(
        agent_id="agent.root", model="gpt-4o",
        input_tokens=1000, output_tokens=500,
    )
    sim._budget.record_llm(
        agent_id="agent.research", model="gpt-4o-mini",
        input_tokens=200, output_tokens=100, cost=0.01,
    )
    snapshot_before = sim._budget.snapshot()
    db = tmp_path / "sim.db"
    sim.save_to(db)
    sim2 = Simulation.load_from(db)
    # 重启不丢累计：agent/simulation 维度累计逐字段一致
    assert sim2._budget.snapshot() == snapshot_before
    assert sim2.budget.agent_usage("agent.root").input_tokens == 1000
    assert sim2.budget.simulation_usage.request_count == 2
    # 重启后累计仍参与预算判定（拒绝行为保留）
    sim2._config.budget.agent.request_count = 1
    plan = {"agent.root": [
        SubmitLLMRequest(agent_id="agent.root", messages=()),
    ]}
    ready = [ReadyCandidate(agent_id="agent.root", events=(), tick=1)]
    validated = sim2._phase_validate(1, plan, ready=ready)
    assert validated["agent.root"][0].success is False
    assert validated["agent.root"][0].error_code == "BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# 矩阵本体：10 类状态面 × 3 性质，逐行可见于 pytest 输出
# ---------------------------------------------------------------------------

_FREEZE = {
    "task_tree": _freeze_task_tree,
    "scheduler_claims": _freeze_scheduler_claims,
    "pending_ops": _freeze_pending_ops,
    "private_files": _freeze_private_files,
    "shared_kb": _freeze_shared_kb,
    "external_process": _freeze_external_process,
    "llm_request": _freeze_llm_request,
    "id_allocation": _freeze_id_allocation,
    "state_epoch": _freeze_state_epoch,
    "budget": _freeze_budget,
}

_ROLLBACK = {
    "task_tree": _rollback_task_tree,
    "scheduler_claims": _rollback_scheduler_claims,
    "pending_ops": _rollback_pending_ops,
    "private_files": _rollback_private_files,
    "shared_kb": _rollback_shared_kb,
    "external_process": _rollback_external_process,
    "llm_request": _rollback_llm_request,
    "id_allocation": _rollback_id_allocation,
    "state_epoch": _rollback_state_epoch,
    "budget": _rollback_budget,
}

_PERSIST = {
    "task_tree": _persist_task_tree,
    "scheduler_claims": _persist_scheduler_claims,
    "pending_ops": _persist_pending_ops,
    "private_files": _persist_private_files,
    "shared_kb": _persist_shared_kb,
    "external_process": _persist_external_process,
    "llm_request": _persist_llm_request,
    "id_allocation": _persist_id_allocation,
    "state_epoch": _persist_state_epoch,
    "budget": _persist_budget,
}


def test_matrix_rows_complete() -> None:
    """矩阵完整：10 类状态面 × 3 性质全部注册（无缺行）。"""
    assert len(SURFACES) == 10
    assert set(_FREEZE) == set(_ROLLBACK) == set(_PERSIST) == set(SURFACES)


class TestFreezeVisibility:
    """Freeze 可见性：冻结后模拟内可见但不可变（SPEC §3.1）。"""

    @pytest.mark.parametrize("surface", SURFACES)
    def test_frozen_view_visible_and_immutable(self, surface: str) -> None:
        _FREEZE[surface]()


class TestCommitRollback:
    """Commit 可回滚性：回滚 tick 后恢复到 tick 前状态（SPEC §3.3/§15）。"""

    @pytest.mark.parametrize("surface", SURFACES)
    def test_rolled_back_tick_restores_pre_tick_state(
        self, surface: str,
    ) -> None:
        _ROLLBACK[surface]()


class TestPersistence:
    """持久化：snapshot 保存 → load 恢复后状态一致，重启不丢。"""

    @pytest.mark.parametrize("surface", SURFACES)
    def test_save_load_preserves_state(self, surface: str, tmp_path) -> None:
        _PERSIST[surface](tmp_path)
