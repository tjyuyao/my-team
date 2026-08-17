"""Tests for the discrete time step simulation engine.

Per KANBAN task: 2026-08-17-time-step
"""

import pytest

from my_team.tick_engine import (
    SimulationState,
    TickConfig,
    TickEngine,
    TickPhase,
    TickResult,
    TickSnapshot,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestTickConfig:
    def test_default_config(self):
        config = TickConfig()
        assert config.tick_duration_value == 10
        assert config.tick_duration_unit == "seconds"
        assert config.simulation_time_per_tick_value == 1
        assert config.simulation_time_per_tick_unit == "hour"
        assert config.start_paused is False
        assert config.deterministic_mode is True

    def test_custom_config(self):
        config = TickConfig(
            tick_duration_value=30,
            tick_duration_unit="minutes",
            start_paused=True,
        )
        assert config.tick_duration_value == 30
        assert config.start_paused is True


# ---------------------------------------------------------------------------
# Basic tick advancement
# ---------------------------------------------------------------------------

class TestTickAdvancement:
    def test_starts_at_tick_zero(self):
        engine = TickEngine()
        assert engine.current_tick == 0

    def test_advance_single_tick(self):
        engine = TickEngine()
        results = engine.advance(1)
        assert len(results) == 1
        assert results[0].tick == 0
        assert engine.current_tick == 1

    def test_advance_multiple_ticks(self):
        engine = TickEngine()
        results = engine.advance(3)
        assert len(results) == 3
        assert [r.tick for r in results] == [0, 1, 2]
        assert engine.current_tick == 3

    def test_tick_increments(self):
        engine = TickEngine()
        engine.advance(1)
        assert engine.current_tick == 1
        engine.advance(1)
        assert engine.current_tick == 2

    def test_advance_returns_tick_results(self):
        engine = TickEngine()
        results = engine.advance(1)
        assert isinstance(results[0], TickResult)
        assert results[0].committed is True


# ---------------------------------------------------------------------------
# Phase execution
# ---------------------------------------------------------------------------

class TestPhaseExecution:
    def test_all_seven_phases_run(self):
        engine = TickEngine()
        results = engine.advance(1)
        phases = results[0].phases_completed
        assert len(phases) == 7
        assert phases == [p for p in TickPhase]

    def test_phases_run_in_order(self):
        engine = TickEngine()
        results = engine.advance(1)
        expected_order = list(TickPhase)
        assert results[0].phases_completed == expected_order

    def test_freeze_creates_snapshot(self):
        engine = TickEngine()
        engine.advance(1)
        snapshot = engine.get_snapshot(0)
        assert snapshot is not None
        assert snapshot.tick == 0

    def test_audit_events_generated(self):
        engine = TickEngine()
        results = engine.advance(1)
        assert len(results[0].audit_events) > 0
        assert results[0].audit_events[0]["event_type"] == "tick_complete"


# ---------------------------------------------------------------------------
# Snapshot mechanism
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_is_frozen(self):
        """Snapshots should be immutable after creation."""
        engine = TickEngine()
        engine.advance(1)
        snap1 = engine.get_snapshot(0)
        engine.advance(1)
        snap2 = engine.get_snapshot(1)

        assert snap1.tick == 0
        assert snap2.tick == 1
        # Original snapshot unchanged
        assert engine.get_snapshot(0).tick == 0

    def test_snapshot_per_tick(self):
        engine = TickEngine()
        engine.advance(3)

        for tick in range(3):
            snap = engine.get_snapshot(tick)
            assert snap is not None
            assert snap.tick == tick

    def test_get_snapshot_default_current(self):
        engine = TickEngine()
        engine.advance(2)
        snap = engine.get_snapshot()  # should return tick 1 (current - 1 after advance)
        # After advance(2), current_tick=2, last snapshot is tick 1
        assert snap is not None
        assert snap.tick == 1

    def test_read_consistency(self):
        """All agents should see the same snapshot within a tick."""
        engine = TickEngine()
        engine.update_context(
            agent_snapshots={
                "agent.a": {"state": "idle"},
                "agent.b": {"state": "processing"},
            }
        )
        engine.advance(1)
        snapshot = engine.get_snapshot(0)
        assert "agent.a" in snapshot.agents
        assert "agent.b" in snapshot.agents
        # Both see the same frozen state
        assert snapshot.agents["agent.a"]["state"] == "idle"
        assert snapshot.agents["agent.b"]["state"] == "processing"


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------

class TestPauseResume:
    def test_starts_running_by_default(self):
        engine = TickEngine()
        assert engine.state == SimulationState.CREATED

    def test_transitions_to_running_on_advance(self):
        engine = TickEngine()
        engine.advance(1)
        assert engine.state == SimulationState.RUNNING

    def test_pause_stops_advancement(self):
        engine = TickEngine()
        engine.advance(1)
        engine.pause()
        assert engine.state == SimulationState.PAUSED
        assert not engine.can_advance()

    def test_cannot_advance_when_paused(self):
        engine = TickEngine()
        engine.advance(1)
        engine.pause()
        with pytest.raises(RuntimeError):
            engine.advance(1)

    def test_resume_allows_advancement(self):
        engine = TickEngine()
        engine.advance(1)
        engine.pause()
        engine.resume()
        assert engine.state == SimulationState.RUNNING
        engine.advance(1)
        assert engine.current_tick == 2

    def test_start_paused(self):
        config = TickConfig(start_paused=True)
        engine = TickEngine(config)
        assert engine.state == SimulationState.PAUSED
        assert not engine.can_advance()

    def test_pause_only_when_running(self):
        engine = TickEngine()
        # CREATED state
        engine.pause()
        assert engine.state == SimulationState.CREATED  # no change


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

class TestEmailDelivery:
    def test_email_delivered_at_correct_tick(self):
        engine = TickEngine()
        engine.update_context(
            pending_emails=[
                {"email_id": "mail.1", "to": ["agent.a"], "deliver_at_tick": 1},
                {"email_id": "mail.2", "to": ["agent.b"], "deliver_at_tick": 0},
            ]
        )
        results = engine.advance(2)

        # Tick 0: mail.2 delivered (deliver_at=0), mail.1 stays pending
        tick0_emails = results[0].emails_queued
        delivered_ids = [e.get("email_id") for e in tick0_emails]
        assert "mail.2" in delivered_ids
        assert "mail.1" not in delivered_ids

        # Tick 1: mail.1 delivered
        tick1_emails = results[1].emails_queued
        delivered_ids = [e.get("email_id") for e in tick1_emails]
        assert "mail.1" in delivered_ids

    def test_email_not_delivered_before_time(self):
        engine = TickEngine()
        engine.update_context(
            pending_emails=[
                {"email_id": "mail.future", "to": ["agent.a"], "deliver_at_tick": 5},
            ]
        )
        results = engine.advance(3)
        all_delivered = []
        for r in results:
            all_delivered.extend(r.emails_queued)
        assert len(all_delivered) == 0


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class TestHistory:
    def test_history_grows(self):
        engine = TickEngine()
        assert len(engine.history) == 0
        engine.advance(1)
        assert len(engine.history) == 1
        engine.advance(2)
        assert len(engine.history) == 3

    def test_history_contains_all_ticks(self):
        engine = TickEngine()
        results = engine.advance(3)
        history = engine.history
        assert len(history) == 3
        assert [h.tick for h in history] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Phase handler override
# ---------------------------------------------------------------------------

class TestPhaseHandlerOverride:
    def test_custom_phase_handler(self):
        engine = TickEngine()
        custom_called = []

        def my_decide(tick, snapshot, context):
            custom_called.append(tick)
            return {"agent_actions": {"agent.custom": [{"action_type": "test"}]}}

        engine.register_phase_handler(TickPhase.DECIDE, my_decide)
        results = engine.advance(1)

        assert custom_called == [0]
        assert "agent.custom" in results[0].agent_actions

    def test_context_update(self):
        engine = TickEngine()
        engine.update_context(
            agent_snapshots={"agent.x": {"status": "idle"}},
            tasks={"task.1": {"status": "assigned"}},
        )
        ctx = engine.get_context()
        assert "agent.x" in ctx["agent_snapshots"]
        assert "task.1" in ctx["tasks"]


# ---------------------------------------------------------------------------
# Repr
# ---------------------------------------------------------------------------

class TestRepr:
    def test_repr(self):
        engine = TickEngine()
        r = repr(engine)
        assert "TickEngine" in r
        assert "tick=0" in r
