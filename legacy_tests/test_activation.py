"""Tests for activation models and AgentScheduler.

Covers: WakeCondition matching, event claiming, ReadyCandidate merging,
max 1 activation per tick, deterministic ordering, bootstrap logic.
"""

import pytest

from my_team.agent_state import AgentState
from my_team.models.activation import (
    AgentActivation,
    ExecutionConfig,
    WakeCondition,
    WakeEventType,
    WakeupEvent,
)
from my_team.scheduler import AgentScheduler, EventStatus

# ---------------------------------------------------------------------------
# WakeCondition
# ---------------------------------------------------------------------------

class TestWakeCondition:
    def test_defaults(self):
        cond = WakeCondition()
        assert WakeEventType.BOOTSTRAP in cond.event_types
        assert cond.wake_at_tick == 0
        assert len(cond.task_ids) == 0
        assert len(cond.resources) == 0

    def test_empty_sets_match_all(self):
        """Empty task_ids/resources means 'no restriction'."""
        cond = WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
            task_ids=set(),
            resources=set(),
        )
        event = WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=1,
            visible_at_tick=1,
            task_id="any.task",
            resource="any/resource",
        )
        from my_team.scheduler import _matches
        assert _matches(cond, event, tick=1)

    def test_restricted_task_ids(self):
        cond = WakeCondition(
            event_types={WakeEventType.CHILD_TASK_CHANGE},
            task_ids={"task.001"},
        )
        from my_team.scheduler import _matches
        match = WakeupEvent(
            event_type=WakeEventType.CHILD_TASK_CHANGE,
            target_agent_id="agent.a",
            tick=1,
            visible_at_tick=1,
            task_id="task.001",
        )
        no_match = WakeupEvent(
            event_type=WakeEventType.CHILD_TASK_CHANGE,
            target_agent_id="agent.a",
            tick=1,
            visible_at_tick=1,
            task_id="task.999",
        )
        assert _matches(cond, match, tick=1)
        assert not _matches(cond, no_match, tick=1)

    def test_restricted_sender_ids(self):
        cond = WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
            sender_ids={"agent.root"},
        )
        from my_team.scheduler import _matches
        match = WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=1,
            visible_at_tick=1,
            source_agent_id="agent.root",
        )
        no_match = WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=1,
            visible_at_tick=1,
            source_agent_id="agent.other",
        )
        assert _matches(cond, match, tick=1)
        assert not _matches(cond, no_match, tick=1)


# ---------------------------------------------------------------------------
# WakeupEvent
# ---------------------------------------------------------------------------

class TestWakeupEvent:
    def test_auto_event_id(self):
        e1 = WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=1,
        )
        e2 = WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=1,
        )
        assert e1.event_id != e2.event_id

    def test_fields(self):
        e = WakeupEvent(
            event_type=WakeEventType.TOOL_RESULT,
            target_agent_id="agent.b",
            tick=5,
            source_agent_id="agent.a",
            task_id="task.001",
            resource="project/report.md",
        )
        assert e.event_type == WakeEventType.TOOL_RESULT
        assert e.target_agent_id == "agent.b"
        assert e.tick == 5


# ---------------------------------------------------------------------------
# AgentActivation
# ---------------------------------------------------------------------------

class TestAgentActivation:
    def test_auto_id(self):
        a1 = AgentActivation(agent_id="agent.a", tick=1)
        a2 = AgentActivation(agent_id="agent.a", tick=1)
        assert a1.activation_id != a2.activation_id

    def test_defaults(self):
        a = AgentActivation(agent_id="agent.a", tick=1)
        assert not a.completed
        assert a.llm_calls == 0
        assert a.tool_calls == 0
        assert a.error is None


# ---------------------------------------------------------------------------
# ExecutionConfig
# ---------------------------------------------------------------------------

class TestExecutionConfig:
    def test_defaults(self):
        cfg = ExecutionConfig()
        assert cfg.max_llm_calls_per_activation == 1
        assert cfg.max_tool_calls_per_activation == 8
        assert cfg.max_action_budget == 32

    def test_bounds_enforced(self):
        with pytest.raises(Exception):
            ExecutionConfig(max_llm_calls_per_activation=100)  # > 16
        with pytest.raises(Exception):
            ExecutionConfig(max_action_budget=0)  # < 1


# ---------------------------------------------------------------------------
# AgentScheduler
# ---------------------------------------------------------------------------

class TestAgentScheduler:
    def test_register_and_ready(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.BOOTSTRAP},
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        assert len(ready) == 1
        assert ready[0].agent_id == "agent.a"

    def test_idle_agent_without_event_not_scheduled(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
        ))
        # No events enqueued
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        assert len(ready) == 0

    def test_paused_agent_not_scheduled(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.BOOTSTRAP},
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.PAUSED})
        assert len(ready) == 0

    def test_future_event_not_scheduled(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=5, visible_at_tick=5,
        ))
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        assert len(ready) == 0

    def test_wake_at_tick_boundary(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.BOOTSTRAP},
            wake_at_tick=3,
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        # tick 2: not yet eligible
        ready = sched.compute_ready_set(2, {"agent.a": AgentState.IDLE})
        assert len(ready) == 0
        # tick 3: eligible
        ready = sched.compute_ready_set(3, {"agent.a": AgentState.IDLE})
        assert len(ready) == 1

    def test_multiple_events_merged(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.NEW_EMAIL, WakeEventType.CHILD_TASK_CHANGE},
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.CHILD_TASK_CHANGE,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        assert len(ready) == 1
        assert len(ready[0].events) == 2

    def test_max_one_activation_per_tick(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.BOOTSTRAP},
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        candidate = ready[0]
        sched.begin_activation(candidate, 0)
        with pytest.raises(ValueError, match="already has an activation"):
            sched.begin_activation(candidate, 0)

    def test_event_claiming(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
        ))
        event = WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        )
        sched.enqueue_event(event)
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        candidate = ready[0]
        act = sched.begin_activation(candidate, 0)

        # Events should be claimed
        for qe in sched.all_events():
            if qe.event.event_id == event.event_id:
                assert qe.status == EventStatus.CLAIMED

        # Complete activation → events consumed
        sched.complete_activation(act.activation_id, success=True)
        for qe in sched.all_events():
            if qe.event.event_id == event.event_id:
                assert qe.status == EventStatus.CONSUMED

    def test_activation_failure_defers_events(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
        ))
        event = WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        )
        sched.enqueue_event(event)
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        act = sched.begin_activation(ready[0], 0)
        sched.complete_activation(act.activation_id, success=False, error="test error")

        # Events should be deferred (eligible again)
        for qe in sched.all_events():
            if qe.event.event_id == event.event_id:
                assert qe.status == EventStatus.ELIGIBLE

    def test_deterministic_ordering(self):
        sched = AgentScheduler()
        for aid in ["agent.c", "agent.a", "agent.b"]:
            sched.register_agent(aid, WakeCondition(
                event_types={WakeEventType.BOOTSTRAP},
            ))
            sched.enqueue_event(WakeupEvent(
                event_type=WakeEventType.BOOTSTRAP,
                target_agent_id=aid,
                tick=0, visible_at_tick=0,
            ))
        states = {aid: AgentState.IDLE for aid in ["agent.a", "agent.b", "agent.c"]}
        ready = sched.compute_ready_set(0, states)
        ids = [c.agent_id for c in ready]
        assert ids == ["agent.a", "agent.b", "agent.c"]

    def test_different_agents_independent(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
        ))
        sched.register_agent("agent.b", WakeCondition(
            event_types={WakeEventType.CHILD_TASK_CHANGE},
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        # agent.b has no matching event
        ready = sched.compute_ready_set(0, {
            "agent.a": AgentState.IDLE,
            "agent.b": AgentState.IDLE,
        })
        ids = [c.agent_id for c in ready]
        assert ids == ["agent.a"]

    def test_update_wake_condition(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types=set(),
        ))
        # No events match initially
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        assert len(ready) == 0

        # Update condition
        sched.update_wake_condition("agent.a", WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
        ))
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        assert len(ready) == 1

    def test_end_tick_expires_eligible_events(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.NEW_EMAIL},
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        # Make eligible but don't schedule
        sched.compute_ready_set(0, {"agent.a": AgentState.PAUSED})
        sched.end_tick()
        # Event should be expired
        for qe in sched.all_events():
            assert qe.status == EventStatus.EXPIRED

    def test_activation_history(self):
        sched = AgentScheduler()
        sched.register_agent("agent.a", WakeCondition(
            event_types={WakeEventType.BOOTSTRAP},
        ))
        sched.enqueue_event(WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.a",
            tick=0, visible_at_tick=0,
        ))
        ready = sched.compute_ready_set(0, {"agent.a": AgentState.IDLE})
        act = sched.begin_activation(ready[0], 0)
        sched.complete_activation(act.activation_id, success=True)
        history = sched.get_activation_history()
        assert len(history) == 1
        assert history[0].agent_id == "agent.a"
        assert history[0].completed
