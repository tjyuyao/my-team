"""Tests for granular waiting states in the agent state machine.

Covers: PROCESSING → WAITING_FOR_* → PROCESSING cycles,
IDLE cannot go directly to WAITING_FOR_*, all waiting states → BLOCKED/FAILED/PAUSED.
"""

import pytest

from my_team.agent_state import (
    AgentState,
    AgentStateMachine,
    InvalidTransitionError,
)


class TestGranularWaitingStates:
    """Verify all new waiting states can be transitioned to from PROCESSING."""

    def test_processing_to_waiting_for_llm(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_llm()
        assert sm.state == AgentState.WAITING_FOR_LLM

    def test_processing_to_waiting_for_tool(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_tool()
        assert sm.state == AgentState.WAITING_FOR_TOOL

    def test_processing_to_waiting_for_child(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_child()
        assert sm.state == AgentState.WAITING_FOR_CHILD

    def test_processing_to_waiting_for_mail(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_mail()
        assert sm.state == AgentState.WAITING_FOR_MAIL

    def test_processing_to_waiting_for_lock(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_lock()
        assert sm.state == AgentState.WAITING_FOR_LOCK

    def test_processing_to_waiting_for_human(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_human()
        assert sm.state == AgentState.WAITING_FOR_HUMAN


class TestWaitingStateTransitions:
    """Verify WAITING_FOR_* → PROCESSING cycles."""

    def test_llm_result_arrives(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_llm()
        sm.resume_from_wait()
        assert sm.state == AgentState.PROCESSING

    def test_tool_result_arrives(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_tool()
        sm.resume_from_wait()
        assert sm.state == AgentState.PROCESSING

    def test_child_completes(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_child()
        sm.resume_from_wait()
        assert sm.state == AgentState.PROCESSING

    def test_mail_arrives(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_mail()
        sm.resume_from_wait()
        assert sm.state == AgentState.PROCESSING

    def test_lock_acquired(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_lock()
        sm.resume_from_wait()
        assert sm.state == AgentState.PROCESSING

    def test_human_responds(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_human()
        sm.resume_from_wait()
        assert sm.state == AgentState.PROCESSING


class TestWaitingToTerminal:
    """Verify WAITING_FOR_* → BLOCKED/FAILED/PAUSED."""

    def test_waiting_for_tool_to_blocked(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_tool()
        sm.block()
        assert sm.state == AgentState.BLOCKED

    def test_waiting_for_child_to_failed(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_child()
        sm.fail()
        assert sm.state == AgentState.FAILED

    def test_waiting_for_mail_to_paused(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_mail()
        sm.pause()
        assert sm.state == AgentState.PAUSED

    def test_waiting_for_llm_to_blocked(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_llm()
        sm.block()
        assert sm.state == AgentState.BLOCKED

    def test_waiting_for_lock_to_failed(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_lock()
        sm.fail()
        assert sm.state == AgentState.FAILED

    def test_waiting_for_human_to_paused(self):
        sm = AgentStateMachine("agent.a", AgentState.PROCESSING)
        sm.wait_for_human()
        sm.pause()
        assert sm.state == AgentState.PAUSED


class TestInvalidWaitingTransitions:
    """Verify IDLE cannot go directly to any WAITING_FOR_* state."""

    def test_idle_to_waiting_for_llm(self):
        sm = AgentStateMachine("agent.a", AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.wait_for_llm()

    def test_idle_to_waiting_for_tool(self):
        sm = AgentStateMachine("agent.a", AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.wait_for_tool()

    def test_idle_to_waiting_for_child(self):
        sm = AgentStateMachine("agent.a", AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.wait_for_child()

    def test_idle_to_waiting_for_mail(self):
        sm = AgentStateMachine("agent.a", AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.wait_for_mail()

    def test_idle_to_waiting_for_lock(self):
        sm = AgentStateMachine("agent.a", AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.wait_for_lock()

    def test_idle_to_waiting_for_human(self):
        sm = AgentStateMachine("agent.a", AgentState.IDLE)
        with pytest.raises(InvalidTransitionError):
            sm.wait_for_human()

    def test_terminated_cannot_transition(self):
        sm = AgentStateMachine("agent.a", AgentState.TERMINATED)
        with pytest.raises(InvalidTransitionError):
            sm.wait_for_llm()


class TestIdleReadyCycle:
    """Verify IDLE → READY → PROCESSING cycle (event-driven scheduling)."""

    def test_idle_to_ready(self):
        sm = AgentStateMachine("agent.a", AgentState.IDLE)
        sm.wake_up()
        assert sm.state == AgentState.READY

    def test_ready_to_processing(self):
        sm = AgentStateMachine("agent.a", AgentState.READY)
        sm.begin_processing()
        assert sm.state == AgentState.PROCESSING

    def test_full_cycle(self):
        sm = AgentStateMachine("agent.a", AgentState.IDLE)
        sm.wake_up()       # IDLE → READY
        sm.begin_processing()  # READY → PROCESSING
        sm.wait_for_child()    # PROCESSING → WAITING_FOR_CHILD
        sm.resume_from_wait()  # WAITING_FOR_CHILD → PROCESSING
        sm.finish_processing()  # PROCESSING → IDLE
        assert sm.state == AgentState.IDLE

    def test_all_states_in_phase_running(self):
        """All new waiting states should be in PHASE_RUNNING."""
        from my_team.agent_state import PHASE_RUNNING
        assert AgentState.WAITING_FOR_LLM in PHASE_RUNNING
        assert AgentState.WAITING_FOR_TOOL in PHASE_RUNNING
        assert AgentState.WAITING_FOR_CHILD in PHASE_RUNNING
        assert AgentState.WAITING_FOR_MAIL in PHASE_RUNNING
        assert AgentState.WAITING_FOR_LOCK in PHASE_RUNNING
        assert AgentState.WAITING_FOR_HUMAN in PHASE_RUNNING
