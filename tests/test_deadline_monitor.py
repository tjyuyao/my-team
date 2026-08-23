"""Tests for T11 deadline monitoring (SPEC §9.2).

Covers:
- DEADLINE_APPROACHING fires once within the configured threshold
- TIMER_EXPIRY fires at/after the real-time deadline, once
- No spurious wakes when far from the deadline
- Rollback un-marks fired events so they re-fire (no lost wake)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from my_team.agent_tree import AgentTree
from my_team.models.activation import WakeEventType
from my_team.models.task import TaskStatus
from my_team.simulation import Simulation

_BASE = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _make_sim() -> Simulation:
    sim = Simulation(agent_tree=_make_tree())
    # Deterministic clock: 10 minutes per tick from a fixed anchor.
    engine = sim.tick_engine
    engine._config.tick_duration_value = 10
    engine._config.tick_duration_unit = "minutes"
    engine._anchor = _BASE
    return sim


def _add_task(sim: Simulation, deadline: datetime) -> str:
    sim.task_tree.create(
        task_id="t.1", title="T", assigner_agent_id="agent.root",
        assignee_agent_id="agent.root", deadline=deadline,
        status=TaskStatus.ASSIGNED,
    )
    return "t.1"


def _wakes(sim: Simulation):
    return [
        qe.event for qe in sim._scheduler.all_events()
        if qe.event.event_type in (
            WakeEventType.DEADLINE_APPROACHING,
            WakeEventType.TIMER_EXPIRY,
        )
    ]


class TestDeadlineMonitor:
    def test_no_wake_when_far_from_deadline(self):
        sim = _make_sim()
        _add_task(sim, _BASE + timedelta(hours=5))
        sim._check_deadlines(tick=0)
        assert _wakes(sim) == []

    def test_approaching_fires_once_within_threshold(self):
        sim = _make_sim()
        # Threshold default = 2 ticks = 20 minutes.
        _add_task(sim, _BASE + timedelta(minutes=15))
        sim._check_deadlines(tick=0)
        wakes = _wakes(sim)
        assert len(wakes) == 1
        assert wakes[0].event_type == WakeEventType.DEADLINE_APPROACHING
        assert wakes[0].target_agent_id == "agent.root"
        # Second scan same tick / next tick: no duplicate.
        sim._check_deadlines(tick=1)
        assert len(_wakes(sim)) == 1

    def test_expiry_fires_at_deadline(self):
        sim = _make_sim()
        _add_task(sim, _BASE + timedelta(minutes=10))
        sim._check_deadlines(tick=0)  # now < deadline → approaching
        assert [w.event_type for w in _wakes(sim)] == [
            WakeEventType.DEADLINE_APPROACHING,
        ]
        sim.tick_engine.advance(1)  # now == deadline
        sim._check_deadlines(tick=1)
        types = [w.event_type for w in _wakes(sim)]
        assert WakeEventType.TIMER_EXPIRY in types
        assert len(types) == 2  # approaching + expiry, no duplicates

    def test_terminal_task_not_scanned(self):
        sim = _make_sim()
        tid = _add_task(sim, _BASE - timedelta(hours=1))
        sim.task_tree.update_status(tid, TaskStatus.CANCELLED, tick=0)
        sim._check_deadlines(tick=0)
        assert _wakes(sim) == []

    def test_rollback_unmarks_fires(self):
        sim = _make_sim()
        _add_task(sim, _BASE + timedelta(minutes=15))
        sim._check_deadlines(tick=0)
        assert len(_wakes(sim)) == 1
        # Simulate tick rollback: the fired event's marker is removed,
        # so re-execution re-fires the wake (no lost wake).
        sim._unmark_deadline_fires()
        sim._check_deadlines(tick=1)
        wakes = _wakes(sim)
        assert len(wakes) == 2  # original + re-fired
        assert all(
            w.event_type == WakeEventType.DEADLINE_APPROACHING
            for w in wakes
        )


@pytest.mark.parametrize("ticks,delta,expected", [
    (0, timedelta(minutes=15), "deadline_approaching"),  # within threshold
    (0, timedelta(minutes=45), None),                    # far away
    (1, timedelta(minutes=30), "deadline_approaching"),  # now == deadline - threshold
])
def test_threshold_matrix(ticks, delta, expected):
    sim = _make_sim()
    _add_task(sim, _BASE + delta)
    sim.tick_engine.advance(ticks)
    sim._check_deadlines(tick=ticks)
    wakes = _wakes(sim)
    if expected is None:
        assert wakes == []
    else:
        assert wakes[0].event_type.value == expected
