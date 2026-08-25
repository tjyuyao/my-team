"""Tests for the discrete time step simulation engine.

TickEngine is now a pure clock — phase execution lives in
Simulation.run_tick().  These tests verify clock semantics:
tick counter, state transitions, pause/resume, can_advance.
"""

import pytest

from my_team.tick_engine import (
    SimulationState,
    TickConfig,
    TickEngine,
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
        engine.advance(1)
        assert engine.current_tick == 1

    def test_advance_multiple_ticks(self):
        engine = TickEngine()
        engine.advance(3)
        assert engine.current_tick == 3

    def test_tick_increments(self):
        engine = TickEngine()
        engine.advance(1)
        assert engine.current_tick == 1
        engine.advance(1)
        assert engine.current_tick == 2


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------

class TestPauseResume:
    def test_starts_created_by_default(self):
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

    def test_pause_from_created(self):
        """Pause can be requested before the first tick."""
        engine = TickEngine()
        engine.pause()
        assert engine.state == SimulationState.PAUSED
        assert not engine.can_advance()


# ---------------------------------------------------------------------------
# Repr
# ---------------------------------------------------------------------------

class TestRepr:
    def test_repr(self):
        engine = TickEngine()
        r = repr(engine)
        assert "TickEngine" in r
        assert "tick=0" in r
