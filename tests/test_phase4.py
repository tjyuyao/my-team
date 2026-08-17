"""Tests for Phase 4: Human control interface.

Covers: pause/resume, human email, status view, tick duration.
"""

import pytest

from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType, AuditLog
from my_team.human_control import CommandResult, HumanCommand, HumanControl
from my_team.mailbox import MailSystem
from my_team.models.email import EmailType
from my_team.shared_kb import SharedKB
from my_team.task_tree import TaskTree
from my_team.tick_engine import SimulationState, TickEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def full_system():
    """Create a full system with all components for testing."""
    agent_tree = AgentTree.from_dict({
        "agents": [
            {"agent_id": "agent.root", "display_name": "Root", "role": "root",
             "parent_id": None, "children": ["agent.research"],
             "tools": ["read", "write", "ls", "delegate"], "can_delegate": True},
            {"agent_id": "agent.research", "display_name": "Research", "role": "research",
             "parent_id": "agent.root", "children": [],
             "tools": ["read", "write", "ls"], "can_delegate": False},
        ],
    })
    task_tree = TaskTree()
    mail_system = MailSystem()
    mail_system.register_agent("agent.root")
    mail_system.register_agent("agent.research")
    shared_kb = SharedKB()
    audit_log = AuditLog()
    tick_engine = TickEngine()

    human = HumanControl(
        tick_engine=tick_engine,
        agent_tree=agent_tree,
        task_tree=task_tree,
        mail_system=mail_system,
        shared_kb=shared_kb,
        audit_log=audit_log,
    )
    return {
        "human": human,
        "engine": tick_engine,
        "agent_tree": agent_tree,
        "task_tree": task_tree,
        "mail": mail_system,
        "kb": shared_kb,
        "audit": audit_log,
    }


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------

class TestPauseResume:
    def test_pause(self, full_system):
        result = full_system["human"].pause(reason="testing")
        assert result.success
        assert full_system["engine"].state == SimulationState.PAUSED

    def test_pause_already_paused(self, full_system):
        full_system["human"].pause()
        result = full_system["human"].pause()
        assert not result.success
        assert "already paused" in result.message

    def test_resume(self, full_system):
        full_system["engine"].advance(1)
        full_system["human"].pause()
        result = full_system["human"].resume()
        assert result.success
        assert full_system["engine"].state == SimulationState.RUNNING

    def test_resume_not_paused(self, full_system):
        result = full_system["human"].resume()
        assert not result.success
        assert "Cannot resume" in result.message

    def test_pause_blocks_advance(self, full_system):
        full_system["engine"].advance(1)
        full_system["human"].pause()
        with pytest.raises(RuntimeError):
            full_system["engine"].advance(1)

    def test_resume_allows_advance(self, full_system):
        full_system["engine"].advance(1)
        full_system["human"].pause()
        full_system["human"].resume()
        full_system["engine"].advance(1)
        assert full_system["engine"].current_tick == 2

    def test_pause_audit_logged(self, full_system):
        full_system["human"].pause(reason="manual check")
        entries = full_system["audit"].for_event_type(AuditEventType.HUMAN_PAUSE)
        assert len(entries) == 1
        assert entries[0].details["reason"] == "manual check"


# ---------------------------------------------------------------------------
# Human Email
# ---------------------------------------------------------------------------

class TestHumanEmail:
    def test_send_email(self, full_system):
        result = full_system["human"].send_email(
            to=["agent.root"],
            subject="New requirement",
            body="Prioritize cost.",
        )
        assert result.success
        assert "email_id" in result.data

    def test_send_to_invalid_agent(self, full_system):
        result = full_system["human"].send_email(
            to=["agent.nonexistent"],
            subject="Test",
        )
        assert not result.success
        assert "not found" in result.message

    def test_email_delivered(self, full_system):
        full_system["human"].send_email(
            to=["agent.root"],
            subject="Test",
            deliver_at_tick=0,
        )
        full_system["mail"].deliver(0)
        mb = full_system["mail"].get_mailbox("agent.root")
        assert mb.unread_count == 1

    def test_email_is_human_type(self, full_system):
        full_system["human"].send_email(
            to=["agent.root"],
            subject="Test",
            deliver_at_tick=0,
        )
        full_system["mail"].deliver(0)
        mb = full_system["mail"].get_mailbox("agent.root")
        emails = mb.get_by_type(EmailType.HUMAN_MESSAGE)
        assert len(emails) == 1

    def test_email_audit_logged(self, full_system):
        full_system["human"].send_email(
            to=["agent.root"],
            subject="Audit test",
        )
        entries = full_system["audit"].for_event_type(AuditEventType.HUMAN_EMAIL)
        assert len(entries) == 1
        assert entries[0].details["subject"] == "Audit test"

    def test_send_to_multiple_agents(self, full_system):
        result = full_system["human"].send_email(
            to=["agent.root", "agent.research"],
            subject="Broadcast",
        )
        assert result.success


# ---------------------------------------------------------------------------
# Status View
# ---------------------------------------------------------------------------

class TestStatusView:
    def test_view_simulation_status(self, full_system):
        result = full_system["human"].execute(
            HumanCommand(command="view_status")
        )
        assert result.success
        assert result.data["tick"] == 0
        assert result.data["agent_count"] == 2

    def test_view_agent_tree(self, full_system):
        result = full_system["human"].view_agent_tree()
        assert "agents" in result
        assert len(result["agents"]) == 2

    def test_view_task_tree(self, full_system):
        full_system["task_tree"].create(
            task_id="t1", title="Task 1",
            creator_agent_id="agent.root", owner_agent_id="agent.research",
        )
        result = full_system["human"].view_task_tree()
        assert result["count"] == 1

    def test_view_locks(self, full_system):
        result = full_system["human"].view_locks()
        assert result["count"] == 0

    def test_view_locks_with_active(self, full_system):
        full_system["kb"].locks.acquire("resource/a", "agent.root", current_tick=0)
        result = full_system["human"].view_locks()
        assert result["count"] == 1

    def test_view_agent_status(self, full_system):
        result = full_system["human"].view_agent_status("agent.root")
        assert result is not None
        assert result["agent_id"] == "agent.root"
        assert result["role"] == "root"

    def test_view_agent_status_not_found(self, full_system):
        result = full_system["human"].view_agent_status("agent.nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Tick Duration
# ---------------------------------------------------------------------------

class TestTickDuration:
    def test_set_immediate(self, full_system):
        result = full_system["human"].set_tick_duration(value=30, unit="seconds")
        assert result.success
        assert "next tick" in result.message

    def test_set_scheduled(self, full_system):
        result = full_system["human"].set_tick_duration(
            value=60, unit="seconds", effective_tick=10,
        )
        assert result.success
        assert "tick 10" in result.message

    def test_set_invalid_value(self, full_system):
        result = full_system["human"].set_tick_duration(value=0)
        assert not result.success
        assert "positive" in result.message

    def test_set_past_tick(self, full_system):
        full_system["engine"].advance(5)
        result = full_system["human"].set_tick_duration(
            value=10, effective_tick=3,
        )
        assert not result.success
        assert "future" in result.message

    def test_duration_change_applied(self, full_system):
        full_system["human"].set_tick_duration(value=30, unit="seconds")
        full_system["engine"].advance(1)  # tick 0
        full_system["human"].apply_pending_duration_changes()
        assert full_system["engine"].config.tick_duration_value == 30

    def test_duration_change_audit_logged(self, full_system):
        full_system["human"].set_tick_duration(value=30, unit="seconds")
        entries = full_system["audit"].for_event_type(AuditEventType.HUMAN_CONFIG_CHANGE)
        assert len(entries) == 1
        assert entries[0].details["value"] == 30


# ---------------------------------------------------------------------------
# Command Router
# ---------------------------------------------------------------------------

class TestCommandRouter:
    def test_execute_pause(self, full_system):
        result = full_system["human"].execute(HumanCommand(command="pause"))
        assert result.success
        assert full_system["engine"].state == SimulationState.PAUSED

    def test_execute_resume(self, full_system):
        full_system["engine"].advance(1)
        full_system["human"].execute(HumanCommand(command="pause"))
        result = full_system["human"].execute(HumanCommand(command="resume"))
        assert result.success

    def test_execute_view_status(self, full_system):
        result = full_system["human"].execute(HumanCommand(command="view_status"))
        assert result.success
        assert "tick" in result.data

    def test_execute_unknown_command(self, full_system):
        result = full_system["human"].execute(HumanCommand(command="nonexistent"))
        assert not result.success
        assert "Unknown" in result.message

    def test_execute_send_email(self, full_system):
        result = full_system["human"].execute(HumanCommand(
            command="send_email",
            params={"to": ["agent.root"], "subject": "Test"},
        ))
        assert result.success
