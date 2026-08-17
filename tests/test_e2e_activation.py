"""End-to-end tests for event-driven scheduling via Simulation.run().

Tests the full tick cycle with the scheduler integration:
bootstrap → email delivery → agent activation → wake events.
"""


import pytest

from my_team.agent_tree import AgentTree
from my_team.models.activation import WakeEventType
from my_team.simulation import Simulation, SimulationConfig


@pytest.fixture
def two_agent_tree():
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root Agent",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.worker"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.worker",
                "display_name": "Worker Agent",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


class TestE2EBootstrap:
    def test_bootstrap_activates_only_root(self, two_agent_tree):
        """Only agents with bootstrap=True should activate on tick 0."""
        sim = Simulation(agent_tree=two_agent_tree)
        sim.run_tick()

        # Check activation history
        history = sim.scheduler.get_activation_history()
        activated_ids = [a.agent_id for a in history]
        assert "agent.root" in activated_ids
        assert "agent.worker" not in activated_ids

    def test_worker_not_activated_without_event(self, two_agent_tree):
        """Worker should not activate when it has no wake events."""
        sim = Simulation(agent_tree=two_agent_tree)
        sim.run_tick()  # tick 0: root bootstraps

        history = sim.scheduler.get_activation_history()
        worker_activations = [a for a in history if a.agent_id == "agent.worker"]
        assert len(worker_activations) == 0

    def test_multiple_ticks_no_spurious_activations(self, two_agent_tree):
        """Running multiple ticks should not activate idle agents."""
        sim = Simulation(agent_tree=two_agent_tree)
        sim.run(max_ticks=5)

        history = sim.scheduler.get_activation_history()
        # Only root should have activated (on tick 0 bootstrap)
        activated_ids = {a.agent_id for a in history}
        assert activated_ids == {"agent.root"}


class TestE2ETickPhases:
    def test_run_completes_all_phases(self, two_agent_tree):
        """Each tick should complete without errors."""
        sim = Simulation(agent_tree=two_agent_tree)
        result = sim.run_tick()
        assert result.tick == 0
        assert sim.current_tick == 1

    def test_audit_log_has_activation_events(self, two_agent_tree):
        """Audit log should record AGENT_ACTIVATED events."""
        sim = Simulation(agent_tree=two_agent_tree)
        sim.run_tick()

        from my_team.audit import AuditEventType
        activated_events = sim.audit_log.for_event_type(AuditEventType.AGENT_ACTIVATED)
        assert len(activated_events) >= 1
        assert activated_events[0].agent_id == "agent.root"

    def test_email_delivery_generates_wake_event(self, two_agent_tree):
        """Email delivery should generate NEW_EMAIL wake events."""
        sim = Simulation(agent_tree=two_agent_tree)
        # Send a human email to root — created at tick 0, delivered at tick 1
        from my_team.models.email import EmailType
        sim.mail_system.create_email(
            from_agent="human.user",
            to=["agent.root"],
            subject="Test",
            body="Hello",
            email_type=EmailType.HUMAN_MESSAGE,
            tick=0,
            deliver_at_tick=1,
        )
        sim.run_tick()  # tick 0: root bootstraps, email still pending
        sim.run_tick()  # tick 1: email delivered, NEW_EMAIL event generated

        # Check that NEW_EMAIL event was generated
        all_events = sim.scheduler.all_events()
        new_email_events = [
            e for e in all_events
            if e.event.event_type == WakeEventType.NEW_EMAIL
        ]
        assert len(new_email_events) >= 1
        assert new_email_events[0].event.target_agent_id == "agent.root"


class TestE2EAgentScheduler:
    def test_scheduler_registered_agents(self, two_agent_tree):
        """All agents should be registered with the scheduler."""
        sim = Simulation(agent_tree=two_agent_tree)
        assert sim.scheduler.get_wake_condition("agent.root") is not None
        assert sim.scheduler.get_wake_condition("agent.worker") is not None

    def test_root_has_bootstrap_condition(self, two_agent_tree):
        """Root should have BOOTSTRAP in its wake condition."""
        sim = Simulation(agent_tree=two_agent_tree)
        cond = sim.scheduler.get_wake_condition("agent.root")
        assert WakeEventType.BOOTSTRAP in cond.event_types

    def test_worker_has_no_bootstrap(self, two_agent_tree):
        """Worker should NOT have BOOTSTRAP in its wake condition."""
        sim = Simulation(agent_tree=two_agent_tree)
        cond = sim.scheduler.get_wake_condition("agent.worker")
        assert WakeEventType.BOOTSTRAP not in cond.event_types


class TestE2ESimulationConfig:
    def test_execution_config_in_sim(self, two_agent_tree):
        """SimulationConfig should include ExecutionConfig."""
        config = SimulationConfig(
            execution={"execution_mode": "discrete_async", "max_llm_calls_per_activation": 2},
        )
        sim = Simulation(agent_tree=two_agent_tree, config=config)
        assert sim.config.execution.max_llm_calls_per_activation == 2

    def test_scheduler_uses_execution_config(self, two_agent_tree):
        """Scheduler should use the execution config from SimulationConfig."""
        config = SimulationConfig(
            execution={"max_action_budget": 16},
        )
        sim = Simulation(agent_tree=two_agent_tree, config=config)
        assert sim.scheduler.config.max_action_budget == 16
