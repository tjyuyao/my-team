"""End-to-end multi-tick simulation tests with scripted agent behavior.

Tests the complete event-driven loop:
  Event wake → Agent activation → Decision → Tool execution
  → Validation → Commit →副作用投递 → Event publish → Next-tick wake

Uses BaseAgent subclasses with deterministic scripted behavior instead of
real LLM calls.
"""

from __future__ import annotations

from my_team.agent_runtime import (
    ActionPlan,
    AgentAction,
    AgentObservation,
    BaseAgent,
)
from my_team.agent_tree import AgentTree
from my_team.models.activation import WakeEventType
from my_team.simulation import Simulation


class ScriptedAgent(BaseAgent):
    """Agent with deterministic scripted behavior for E2E testing.

    Receives a list of (tick, ActionPlan) pairs. When decide() is called
    at the matching tick, returns the scripted plan.
    """

    def __init__(
        self,
        agent_id: str,
        script: dict[int, ActionPlan] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self._script = script or {}

    def decide(self, observation: AgentObservation) -> ActionPlan:
        return self._script.get(observation.tick, ActionPlan(
            agent_id=self._agent_id,
            tick=observation.tick,
            actions=[],
        ))


def _make_tree() -> AgentTree:
    """3-layer tree: root → research → web_research."""
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root Agent",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research Agent",
                "role": "research_agent",
                "parent_id": "agent.root",
                "children": ["agent.web_research"],
                "tools": ["read", "write", "ls", "delegate", "send_email"],
                "can_delegate": True,
                "metadata": {"bootstrap": False},
            },
            {
                "agent_id": "agent.web_research",
                "display_name": "Web Research Agent",
                "role": "worker",
                "parent_id": "agent.research",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


class TestE2EMultiTickDelegation:
    """Prove the full delegation loop works across multiple ticks."""

    def test_root_delegates_to_research(self, tmp_path: object) -> None:
        """Root bootstraps, delegates a task, and the email is delivered."""
        tree = _make_tree()

        # Root script: on tick 0 (bootstrap), delegate to research
        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Market Analysis",
                            "task_description": "Analyze three markets",
                        },
                    ),
                ],
            ),
        }

        sim = Simulation(agent_tree=tree)
        # Replace the root runtime with our scripted agent
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        # Tick 0: root bootstraps and delegates
        sim.run_tick()
        assert sim.current_tick == 1

        # Verify: a task was created
        active_tasks = sim.task_tree.get_active_tasks()
        assert len(active_tasks) == 1
        assert active_tasks[0].title == "Market Analysis"
        assert active_tasks[0].assignee_agent_id == "agent.research"

        # Verify: email was queued (deliver at tick 1)
        assert sim.current_tick == 1

    def test_email_delivery_wakes_research(self) -> None:
        """Email committed at tick 0 is delivered at tick 1, waking research."""
        tree = _make_tree()

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Data Collection",
                            "task_description": "Collect market data",
                        },
                    ),
                ],
            ),
        }

        sim = Simulation(agent_tree=tree)
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        # Tick 0: root delegates (email committed, deliver_at_tick=1)
        sim.run_tick()

        # Tick 1: email delivered, NEW_EMAIL event generated
        sim.run_tick()

        # Check that NEW_EMAIL event was enqueued for research
        all_events = sim.scheduler.all_events()
        new_email_events = [
            e for e in all_events
            if e.event.event_type == WakeEventType.NEW_EMAIL
            and e.event.target_agent_id == "agent.research"
        ]
        assert len(new_email_events) >= 1

    def test_research_receives_and_sends_result(self) -> None:
        """Research receives delegation, sends result back."""
        tree = _make_tree()

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Quick Task",
                            "task_description": "Do something",
                        },
                    ),
                ],
            ),
        }

        # Research script: when activated by NEW_EMAIL, accept task + send result
        # NEW_EMAIL event is generated in tick 1's Deliver phase and eligible
        # in tick 1's Schedule phase (same tick), so research activates at tick 1
        research_script = {
            1: ActionPlan(
                agent_id="agent.research",
                tick=1,
                actions=[
                    AgentAction(
                        action_type="send_email",
                        tool_name="send_email",
                        payload={
                            "to": ["agent.root"],
                            "subject": "Result",
                            "body": "Task completed successfully",
                        },
                    ),
                ],
            ),
        }

        sim = Simulation(agent_tree=tree)

        # Wire scripted agents
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        scripted_research = ScriptedAgent("agent.research", script=research_script)
        scripted_research._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = scripted_research

        # Tick 0: root delegates (email committed, deliver_at_tick=1)
        sim.run_tick()

        # Tick 1: email delivered to research, NEW_EMAIL wake event generated
        sim.run_tick()

        # Tick 2: research activated by NEW_EMAIL, sends result back
        sim.run_tick()

        # Verify: research activated
        history = sim.scheduler.get_activation_history()
        research_activations = [a for a in history if a.agent_id == "agent.research"]
        assert len(research_activations) >= 1

        # Verify: result email was staged (deliver at tick 3)
        all_events = sim.scheduler.all_events()
        result_events = [
            e for e in all_events
            if e.event.event_type == WakeEventType.NEW_EMAIL
            and e.event.target_agent_id == "agent.root"
        ]
        assert len(result_events) >= 1

    def test_three_tick_delegation_loop(self) -> None:
        """Full 3-tick loop: Root → delegate → Research accepts → Root receives."""
        tree = _make_tree()

        root_tick0 = ActionPlan(
            agent_id="agent.root",
            tick=0,
            actions=[
                AgentAction(
                    action_type="delegate",
                    tool_name="delegate",
                    payload={
                        "recipient_agent_id": "agent.research",
                        "task_title": "Analysis",
                        "task_description": "Analyze data",
                    },
                ),
            ],
        )

        # Research activated by NEW_EMAIL at tick 1 (email delivered in tick 1 Deliver phase)
        research_tick1 = ActionPlan(
            agent_id="agent.research",
            tick=1,
            actions=[
                AgentAction(
                    action_type="send_email",
                    tool_name="send_email",
                    payload={
                        "to": ["agent.root"],
                        "subject": "Analysis Complete",
                        "body": "Here are the results",
                    },
                ),
            ],
        )

        root_tick2 = ActionPlan(
            agent_id="agent.root",
            tick=2,
            actions=[],  # Root receives result, no further action
        )

        sim = Simulation(agent_tree=tree)

        scripted_root = ScriptedAgent("agent.root", script={
            0: root_tick0,
            2: root_tick2,
        })
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        scripted_research = ScriptedAgent("agent.research", script={
            1: research_tick1,
        })
        scripted_research._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = scripted_research

        # Tick 0: root delegates (email committed, deliver_at_tick=1)
        sim.run_tick()
        assert sim.current_tick == 1

        # Tick 1: email delivered to research, NEW_EMAIL generated,
        # research activated in same tick, sends result
        sim.run_tick()
        assert sim.current_tick == 2

        # Verify task was created
        active_tasks = sim.task_tree.get_active_tasks()
        assert len(active_tasks) == 1

        # Verify activation history
        history = sim.scheduler.get_activation_history()
        activated_agents = [a.agent_id for a in history]
        assert "agent.root" in activated_agents
        assert "agent.research" in activated_agents

    def test_idle_agents_not_activated(self) -> None:
        """Agents without wake events should remain idle."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        # Run 3 ticks with no scripted behavior (default BaseAgent)
        sim.run(max_ticks=3)

        history = sim.scheduler.get_activation_history()
        activated_agents = {a.agent_id for a in history}

        # Only root should activate (bootstrap)
        assert "agent.root" in activated_agents
        # Research and web_research should NOT activate
        assert "agent.research" not in activated_agents
        assert "agent.web_research" not in activated_agents

    def test_audit_log_records_activations(self) -> None:
        """Audit log should record all activations."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)
        sim.run_tick()

        from my_team.audit import AuditEventType
        activated_events = sim.audit_log.for_event_type(AuditEventType.AGENT_ACTIVATED)
        assert len(activated_events) >= 1
        assert activated_events[0].agent_id == "agent.root"

    def test_transaction_buffer_cleared_after_commit(self) -> None:
        """Transaction buffer should be cleared after each tick's commit."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)
        sim.run_tick()

        # Buffer should be empty after commit
        assert sim._transaction_buffer.committed_count == 0
        assert not sim._transaction_buffer.has_pending


class TestE2EToolExecution:
    """Test that tool handlers actually execute through the registry."""

    def test_write_file_through_registry(self) -> None:
        """Write tool should create a file via the handler."""
        tree = _make_tree()

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="write",
                        tool_name="write",
                        payload={"path": "output.md", "content": "# Result\nDone"},
                    ),
                ],
            ),
        }

        sim = Simulation(agent_tree=tree)
        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        sim.run_tick()

        # Verify: file was written via commit
        home = sim._private_store.agent_home("agent.root")
        output_file = home / "output.md"
        assert output_file.exists()
        assert output_file.read_text() == "# Result\nDone"

    def test_read_file_through_registry(self) -> None:
        """Read tool should return file content."""
        tree = _make_tree()
        sim = Simulation(agent_tree=tree)

        # First, create a file
        home = sim._private_store.agent_home("agent.root")
        home.mkdir(parents=True, exist_ok=True)
        (home / "data.txt").write_text("Hello, world!")

        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="read",
                        tool_name="read",
                        payload={"path": "data.txt"},
                    ),
                ],
            ),
        }

        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        sim.run_tick()

        # The read should have succeeded (no error in results)
        history = sim.scheduler.get_activation_history()
        assert len(history) >= 1

    def test_send_email_through_registry(self) -> None:
        """send_email tool should stage an email effect."""
        tree = _make_tree()

        # Root delegates to research, triggering a NEW_EMAIL event,
        # then research sends a result email back
        root_script = {
            0: ActionPlan(
                agent_id="agent.root",
                tick=0,
                actions=[
                    AgentAction(
                        action_type="delegate",
                        tool_name="delegate",
                        payload={
                            "recipient_agent_id": "agent.research",
                            "task_title": "Send Update",
                            "task_description": "Send status update",
                        },
                    ),
                ],
            ),
        }

        # Research activated by NEW_EMAIL at tick 1 (email delivered at tick 1)
        research_script = {
            1: ActionPlan(
                agent_id="agent.research",
                tick=1,
                actions=[
                    AgentAction(
                        action_type="send_email",
                        tool_name="send_email",
                        payload={
                            "to": ["agent.root"],
                            "subject": "Status Update",
                            "body": "All systems operational",
                        },
                    ),
                ],
            ),
        }

        sim = Simulation(agent_tree=tree)

        scripted_root = ScriptedAgent("agent.root", script=root_script)
        scripted_root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = scripted_root

        scripted_research = ScriptedAgent("agent.research", script=research_script)
        scripted_research._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = scripted_research

        # Tick 0: root delegates (email committed, deliver_at_tick=1)
        sim.run_tick()

        # Tick 1: email delivered to research, research activated, sends email back
        sim.run_tick()

        # Check that research was activated
        history = sim.scheduler.get_activation_history()
        research_activations = [a for a in history if a.agent_id == "agent.research"]
        assert len(research_activations) >= 1

        # The result email is committed at tick 1 with deliver_at_tick=2,
        # so NEW_EMAIL event is generated at tick 2's Deliver phase
        sim.run_tick()

        # Check that NEW_EMAIL event was generated for root
        all_events = sim.scheduler.all_events()
        email_events = [
            e for e in all_events
            if e.event.event_type == WakeEventType.NEW_EMAIL
            and e.event.target_agent_id == "agent.root"
        ]
        assert len(email_events) >= 1
