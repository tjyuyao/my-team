"""Tests for T11 决策 2: SLA ready-set ordering + activation capacity.

Covers:
- Ready set ordered by (priority desc, deadline asc [real time], agent_id)
- max_active_agents_per_tick capacity cut
- Over-capacity candidates keep events (requeued) and re-compete next tick
- Urgency derived from TaskTree via Simulation._agent_urgency
"""

from datetime import datetime, timedelta, timezone

import pytest

from my_team.agent_state import AgentState
from my_team.models.activation import (
    ExecutionConfig,
    WakeCondition,
    WakeEventType,
    WakeupEvent,
)
from my_team.models.task import TaskPriority, TaskStatus
from my_team.scheduler import AgentScheduler, EventStatus

_BASE = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)


def _make_scheduler(capacity: int = 8) -> AgentScheduler:
    return AgentScheduler(config=ExecutionConfig(max_active_agents_per_tick=capacity))


def _register_with_event(
    scheduler: AgentScheduler,
    agent_id: str,
    tick: int = 1,
) -> None:
    scheduler.register_agent(agent_id, WakeCondition(
        event_types={WakeEventType.NEW_EMAIL},
    ))
    scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.NEW_EMAIL,
        target_agent_id=agent_id,
        tick=tick,
        visible_at_tick=tick,
    ))


class TestUrgencyOrdering:
    def test_higher_priority_first(self):
        sched = _make_scheduler()
        for aid in ("agent.low", "agent.urgent", "agent.normal"):
            _register_with_event(sched, aid)
        urgency = {
            "agent.low": (0, None),
            "agent.urgent": (3, None),
            "agent.normal": (1, None),
        }
        ready = sched.compute_ready_set(
            1,
            {aid: AgentState.IDLE for aid in urgency},
            urgency=lambda aid: urgency[aid],
        )
        assert [c.agent_id for c in ready] == [
            "agent.urgent", "agent.normal", "agent.low",
        ]

    def test_earlier_deadline_first_within_priority(self):
        sched = _make_scheduler()
        for aid in ("agent.late", "agent.soon"):
            _register_with_event(sched, aid)
        urgency = {
            "agent.late": (1, _BASE + timedelta(hours=5)),
            "agent.soon": (1, _BASE + timedelta(minutes=5)),
        }
        ready = sched.compute_ready_set(
            1,
            {aid: AgentState.IDLE for aid in urgency},
            urgency=lambda aid: urgency[aid],
        )
        assert [c.agent_id for c in ready] == ["agent.soon", "agent.late"]

    def test_no_deadline_sorts_after_deadline_same_priority(self):
        sched = _make_scheduler()
        for aid in ("agent.none", "agent.dated"):
            _register_with_event(sched, aid)
        urgency = {
            "agent.none": (1, None),
            "agent.dated": (1, _BASE + timedelta(days=30)),
        }
        ready = sched.compute_ready_set(
            1,
            {aid: AgentState.IDLE for aid in urgency},
            urgency=lambda aid: urgency[aid],
        )
        assert [c.agent_id for c in ready] == ["agent.dated", "agent.none"]

    def test_no_tasks_sort_last(self):
        sched = _make_scheduler()
        for aid in ("agent.idle", "agent.worker"):
            _register_with_event(sched, aid)
        urgency = {"agent.worker": (1, None), "agent.idle": (-1, None)}
        ready = sched.compute_ready_set(
            1,
            {aid: AgentState.IDLE for aid in urgency},
            urgency=lambda aid: urgency[aid],
        )
        assert [c.agent_id for c in ready] == ["agent.worker", "agent.idle"]

    def test_agent_id_tiebreak_deterministic(self):
        sched = _make_scheduler()
        for aid in ("agent.b", "agent.a"):
            _register_with_event(sched, aid)
        urgency = lambda aid: (1, None)  # noqa: E731 — identical urgency
        ready = sched.compute_ready_set(
            1,
            {aid: AgentState.IDLE for aid in ("agent.a", "agent.b")},
            urgency=urgency,
        )
        assert [c.agent_id for c in ready] == ["agent.a", "agent.b"]


class TestActivationCapacity:
    def test_capacity_cut_keeps_top_urgency(self):
        sched = _make_scheduler(capacity=2)
        for aid in ("agent.a", "agent.b", "agent.c"):
            _register_with_event(sched, aid)
        urgency = {
            "agent.a": (3, None),
            "agent.b": (2, None),
            "agent.c": (1, None),
        }
        states = {aid: AgentState.IDLE for aid in urgency}
        ready = sched.compute_ready_set(1, states, urgency=lambda aid: urgency[aid])
        assert [c.agent_id for c in ready] == ["agent.a", "agent.b"]
        assert [c.agent_id for c in sched.last_overflow] == ["agent.c"]

    def test_overflow_events_survive_and_recompete_next_tick(self):
        """超容者保持就绪，下 tick 再竞争（幂等，无状态损失）."""
        sched = _make_scheduler(capacity=1)
        for aid in ("agent.hi", "agent.lo"):
            _register_with_event(sched, aid)
        urgency = {"agent.hi": (3, None), "agent.lo": (1, None)}
        states = {aid: AgentState.IDLE for aid in urgency}

        ready = sched.compute_ready_set(1, states, urgency=lambda aid: urgency[aid])
        assert [c.agent_id for c in ready] == ["agent.hi"]

        # Overflow event must NOT be expired by end_tick.
        sched.end_tick()
        statuses = {qe.event.target_agent_id: qe.status for qe in sched.all_events()}
        assert statuses["agent.lo"] == EventStatus.QUEUED

        # Next tick: with the high-priority agent gone, the deferred
        # agent re-enters competition and activates.
        ready2 = sched.compute_ready_set(
            2,
            {"agent.lo": AgentState.IDLE},
            urgency=lambda aid: urgency[aid],
        )
        assert [c.agent_id for c in ready2] == ["agent.lo"]

    def test_no_cut_when_under_capacity(self):
        sched = _make_scheduler(capacity=8)
        for aid in ("agent.a", "agent.b"):
            _register_with_event(sched, aid)
        ready = sched.compute_ready_set(
            1,
            {aid: AgentState.IDLE for aid in ("agent.a", "agent.b")},
            urgency=lambda aid: (-1, None),
        )
        assert len(ready) == 2
        assert sched.last_overflow == []


class TestTaskTreeUrgency:
    def test_simulation_urgency_from_task_tree(self):
        """End-to-end urgency derivation: priority then real deadline."""
        from my_team.agent_tree import AgentTree
        from my_team.models.agent import AgentConfig
        from my_team.simulation import Simulation
        from my_team.task_tree import TaskTree

        configs = [
            AgentConfig(
                agent_id="root", display_name="Root", role="root",
                children=["agent.a", "agent.b"],
            ),
            AgentConfig(
                agent_id="agent.a", display_name="A", role="worker",
                parent_id="root",
            ),
            AgentConfig(
                agent_id="agent.b", display_name="B", role="worker",
                parent_id="root",
            ),
        ]
        sim = Simulation(agent_tree=AgentTree(agents=configs))
        tt: TaskTree = sim.task_tree
        # agent.a: URGENT without deadline; agent.b: HIGH with deadline.
        tt.create(
            task_id="t.a", title="A", creator_agent_id="root",
            owner_agent_id="agent.a", priority=TaskPriority.URGENT,
            status=TaskStatus.ASSIGNED,
        )
        tt.create(
            task_id="t.b", title="B", creator_agent_id="root",
            owner_agent_id="agent.b", priority=TaskPriority.HIGH,
            deadline=sim.tick_engine.wall_now() + timedelta(hours=1),
            status=TaskStatus.ASSIGNED,
        )
        rank_a, dl_a = sim._agent_urgency("agent.a")
        rank_b, dl_b = sim._agent_urgency("agent.b")
        assert rank_a == 3 and dl_a is None
        assert rank_b == 2 and dl_b is not None
        # Terminal tasks are ignored.
        tt.update_status("t.a", TaskStatus.CANCELLED, tick=0)
        rank_a2, dl_a2 = sim._agent_urgency("agent.a")
        assert rank_a2 == -1 and dl_a2 is None


@pytest.mark.parametrize("cap", [1, 2, 4])
def test_capacity_is_configurable(cap):
    sched = _make_scheduler(capacity=cap)
    assert sched.config.max_active_agents_per_tick == cap
