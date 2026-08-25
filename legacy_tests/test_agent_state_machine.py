"""Tests for Agent lifecycle state machine.

Per KANBAN task: 2026-08-17-agent-state-machine
"""

import pytest

from my_team.agent_state import (
    AgentState,
    AgentStateMachine,
    AuditLog,
    InvalidTransitionError,
    is_running,
    is_terminal,
)

# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------

class TestStateClassification:
    def test_lifecycle_states(self):
        assert not is_running(AgentState.CREATED)
        assert not is_running(AgentState.INITIALIZED)
        assert not is_running(AgentState.READY)
        assert not is_terminal(AgentState.CREATED)

    def test_running_states(self):
        for state in [AgentState.IDLE, AgentState.PROCESSING, AgentState.WAITING,
                      AgentState.BLOCKED, AgentState.PAUSED, AgentState.FAILED]:
            assert is_running(state)
            assert not is_terminal(state)

    def test_terminal_state(self):
        assert is_terminal(AgentState.TERMINATED)
        assert not is_running(AgentState.TERMINATED)


# ---------------------------------------------------------------------------
# Happy-path lifecycle transitions
# ---------------------------------------------------------------------------

class TestLifecycle:
    """Tests for the happy-path lifecycle: created → initialized → ready → idle → ..."""

    def test_full_lifecycle(self):
        sm = AgentStateMachine("agent.test")
        assert sm.state == AgentState.CREATED

        sm.initialize()
        assert sm.state == AgentState.INITIALIZED

        sm.mark_ready()
        assert sm.state == AgentState.READY

        sm.start()
        assert sm.state == AgentState.IDLE

    def test_processing_cycle(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.IDLE)

        sm.begin_processing()
        assert sm.state == AgentState.PROCESSING

        sm.finish_processing()
        assert sm.state == AgentState.IDLE

    def test_waiting_cycle(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.PROCESSING)

        sm.wait()
        assert sm.state == AgentState.WAITING

        sm.resume_from_wait()
        assert sm.state == AgentState.PROCESSING

    def test_blocked_resolution(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.PROCESSING)

        sm.block()
        assert sm.state == AgentState.BLOCKED

        sm.resolve_block()
        assert sm.state == AgentState.IDLE

    def test_pause_and_unpause(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.IDLE)

        sm.pause()
        assert sm.state == AgentState.PAUSED

        sm.unpause()
        assert sm.state == AgentState.IDLE

    def test_pause_from_processing(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.PROCESSING)

        sm.pause()
        assert sm.state == AgentState.PAUSED

        sm.unpause(target=AgentState.PROCESSING)
        assert sm.state == AgentState.PROCESSING

    def test_failure_and_recovery(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.PROCESSING)

        sm.fail()
        assert sm.state == AgentState.FAILED

        sm.recover()
        assert sm.state == AgentState.IDLE

    def test_failure_to_terminated(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.FAILED)

        sm.terminate()
        assert sm.state == AgentState.TERMINATED
        assert sm.is_terminal

    def test_termination_from_idle(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.IDLE)

        sm.terminate()
        assert sm.state == AgentState.TERMINATED

    def test_termination_from_blocked(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.BLOCKED)

        sm.terminate()
        assert sm.state == AgentState.TERMINATED


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions:
    """Tests that illegal transitions are rejected."""

    def test_cannot_skip_lifecycle(self):
        sm = AgentStateMachine("agent.test")
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition(AgentState.READY)  # skip initialized
        assert exc_info.value.from_state == AgentState.CREATED
        assert exc_info.value.to_state == AgentState.READY

    def test_cannot_go_back_from_ready(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.READY)
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentState.INITIALIZED)

    def test_cannot_transition_from_terminal(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.TERMINATED)
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentState.IDLE)

    def test_cannot_go_directly_to_processing_from_idle(self):
        """idle → processing is valid, but idle → waiting is not."""
        sm = AgentStateMachine("agent.test", initial_state=AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentState.WAITING)

    def test_cannot_block_from_idle(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentState.BLOCKED)

    def test_cannot_unpause_to_created(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.PAUSED)
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentState.CREATED)

    def test_invalid_transition_preserves_state(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentState.BLOCKED)
        assert sm.state == AgentState.IDLE  # state unchanged


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_transitions_are_logged(self):
        log = AuditLog()
        sm = AgentStateMachine("agent.test", audit_log=log)

        sm.initialize()
        sm.mark_ready()
        sm.start()

        assert len(log) == 3
        assert log.entries[0].from_state == AgentState.CREATED
        assert log.entries[0].to_state == AgentState.INITIALIZED
        assert log.entries[1].to_state == AgentState.READY
        assert log.entries[2].to_state == AgentState.IDLE

    def test_audit_records_agent_id(self):
        log = AuditLog()
        sm = AgentStateMachine("agent.research", audit_log=log)

        sm.initialize()

        entry = log.entries[0]
        assert entry.agent_id == "agent.research"

    def test_audit_records_tick(self):
        log = AuditLog()
        sm = AgentStateMachine("agent.test", audit_log=log)

        sm.initialize(tick=5)

        entry = log.entries[0]
        assert entry.tick == 5

    def test_audit_records_reason(self):
        log = AuditLog()
        sm = AgentStateMachine("agent.test", audit_log=log)

        sm.initialize(reason="system startup")

        entry = log.entries[0]
        assert entry.reason == "system startup"

    def test_for_agent_filter(self):
        log = AuditLog()
        sm_a = AgentStateMachine("agent.a", audit_log=log)
        sm_b = AgentStateMachine("agent.b", audit_log=log)

        sm_a.initialize()
        sm_b.initialize()
        sm_a.mark_ready()

        a_entries = log.for_agent("agent.a")
        b_entries = log.for_agent("agent.b")

        assert len(a_entries) == 2
        assert len(b_entries) == 1

    def test_last_for_agent(self):
        log = AuditLog()
        sm = AgentStateMachine("agent.test", audit_log=log)

        sm.initialize()
        sm.mark_ready()

        last = log.last_for_agent("agent.test")
        assert last is not None
        assert last.to_state == AgentState.READY

    def test_last_for_unknown_agent(self):
        log = AuditLog()
        assert log.last_for_agent("agent.nonexistent") is None

    def test_invalid_transition_not_logged(self):
        log = AuditLog()
        sm = AgentStateMachine("agent.test", initial_state=AgentState.IDLE, audit_log=log)
        initial_count = len(log)  # may include init entry

        with pytest.raises(InvalidTransitionError):
            sm.transition(AgentState.BLOCKED)

        assert len(log) == initial_count  # no new entries for failed transitions


# ---------------------------------------------------------------------------
# Transition count & properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_transition_count(self):
        sm = AgentStateMachine("agent.test")
        assert sm.transition_count == 0

        sm.initialize()
        assert sm.transition_count == 1

        sm.mark_ready()
        assert sm.transition_count == 2

    def test_is_running(self):
        sm = AgentStateMachine("agent.test")
        assert not sm.is_running

        sm.initialize()
        sm.mark_ready()
        sm.start()
        assert sm.is_running

    def test_is_terminal(self):
        sm = AgentStateMachine("agent.test")
        assert not sm.is_terminal

        sm.initialize()
        sm.mark_ready()
        sm.start()
        sm.terminate()
        assert sm.is_terminal

    def test_can_transition_to(self):
        sm = AgentStateMachine("agent.test")
        assert sm.can_transition_to(AgentState.INITIALIZED)
        assert not sm.can_transition_to(AgentState.READY)
        assert not sm.can_transition_to(AgentState.IDLE)

    def test_repr(self):
        sm = AgentStateMachine("agent.test")
        r = repr(sm)
        assert "agent.test" in r
        assert "created" in r


# ---------------------------------------------------------------------------
# Custom initial state
# ---------------------------------------------------------------------------

class TestCustomInitialState:
    def test_start_at_idle(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.IDLE)
        assert sm.state == AgentState.IDLE
        assert sm.transition_count == 0  # no transition logged for custom init

    def test_start_at_processing(self):
        sm = AgentStateMachine("agent.test", initial_state=AgentState.PROCESSING)
        assert sm.state == AgentState.PROCESSING

        sm.wait()
        assert sm.state == AgentState.WAITING
