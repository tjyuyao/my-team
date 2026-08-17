"""Tests for AgentRuntime, ToolContext, and Simulation integration.

Covers review gaps §8.1 (Simulation), §8.2 (AgentRuntime), §8.3 (identity).
"""

import json

import pytest

from my_team.agent_runtime import (
    AgentObservation,
    AgentRuntime,
    AgentSnapshot,
    ActionResult,
    ActionContext,
    ActionPlan,
    AgentAction,
    BaseAgent,
    ManagerAgent,
    RootAgent,
    SubAgent,
    ToolContext,
    ToolPermissionError,
    ToolRegistry,
    ToolResult,
    MANAGER_TOOLS,
    ROOT_TOOLS,
    WORKER_TOOLS,
)
from my_team.agent_tree import AgentTree
from my_team.simulation import Simulation, SimulationConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_agent_tree() -> AgentTree:
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
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research Agent",
                "role": "research_manager",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "delegate", "send_email"],
                "can_delegate": True,
            },
        ],
    })


@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_agent("agent.root", ROOT_TOOLS)
    reg.register_agent("agent.research", MANAGER_TOOLS)
    return reg


# ---------------------------------------------------------------------------
# ToolContext
# ---------------------------------------------------------------------------

class TestToolContext:
    def test_immutable(self):
        ctx = ToolContext(agent_id="agent.a", tick=5)
        with pytest.raises(AttributeError):
            ctx.agent_id = "agent.b"  # type: ignore[misc]

    def test_frozen(self):
        ctx = ToolContext(agent_id="agent.a")
        assert ctx.agent_id == "agent.a"
        assert ctx.tick == 0


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_authorize_allowed(self, tool_registry):
        ctx = ToolContext(agent_id="agent.root", allowed_tools=ROOT_TOOLS)
        tool_registry.authorize(ctx, "read")  # should not raise

    def test_authorize_denied(self, tool_registry):
        ctx = ToolContext(agent_id="agent.root", allowed_tools=ROOT_TOOLS)
        with pytest.raises(ToolPermissionError):
            tool_registry.authorize(ctx, "send_email")

    def test_can_use(self, tool_registry):
        assert tool_registry.can_use("agent.root", "read")
        assert not tool_registry.can_use("agent.root", "send_email")
        assert tool_registry.can_use("agent.research", "send_email")

    def test_execute_with_handler(self, tool_registry):
        def my_read(context: ToolContext, path: str = "") -> ToolResult:
            return ToolResult(success=True, data=f"content of {path}")

        tool_registry.register_handler("read", my_read)
        ctx = ToolContext(agent_id="agent.root", allowed_tools=ROOT_TOOLS)
        result = tool_registry.execute(ctx, "read", path="test.md")
        assert result.success
        assert result.data == "content of test.md"

    def test_execute_no_handler(self, tool_registry):
        ctx = ToolContext(agent_id="agent.root", allowed_tools=ROOT_TOOLS)
        # "nonexistent_tool" not in ROOT_TOOLS, so permission denied first
        result = tool_registry.execute(ctx, "nonexistent_tool")
        assert not result.success
        assert "does not have permission" in result.error

    def test_execute_permission_denied(self, tool_registry):
        def handler(context: ToolContext) -> ToolResult:
            return ToolResult(success=True)

        tool_registry.register_handler("send_email", handler)
        ctx = ToolContext(agent_id="agent.root", allowed_tools=ROOT_TOOLS)
        result = tool_registry.execute(ctx, "send_email")
        assert not result.success
        assert "does not have permission" in result.error


# ---------------------------------------------------------------------------
# Root Agent restrictions
# ---------------------------------------------------------------------------

class TestRootAgentRestrictions:
    def test_root_tools_limited(self, tool_registry):
        root = RootAgent(agent_id="agent.root", tool_registry=tool_registry)
        assert root.tool_context.allowed_tools == ROOT_TOOLS

    def test_root_cannot_send_email(self, tool_registry):
        root = RootAgent(agent_id="agent.root", tool_registry=tool_registry)
        assert not tool_registry.can_use("agent.root", "send_email")

    def test_root_can_read_write_ls_delegate(self, tool_registry):
        root = RootAgent(agent_id="agent.root", tool_registry=tool_registry)
        for tool in ["read", "write", "ls", "delegate"]:
            assert tool_registry.can_use("agent.root", tool)

    def test_manager_has_more_tools(self, tool_registry):
        mgr = ManagerAgent(agent_id="agent.research", tool_registry=tool_registry)
        assert "send_email" in mgr.tool_context.allowed_tools
        assert "delegate" in mgr.tool_context.allowed_tools

    def test_worker_default_tools(self, tool_registry):
        worker = SubAgent(agent_id="agent.web", tool_registry=tool_registry)
        assert worker.tool_context.allowed_tools == WORKER_TOOLS

    def test_worker_extra_tools(self, tool_registry):
        worker = SubAgent(
            agent_id="agent.web",
            extra_tools=frozenset({"web_search"}),
            tool_registry=tool_registry,
        )
        assert "web_search" in worker.tool_context.allowed_tools


# ---------------------------------------------------------------------------
# AgentSnapshot
# ---------------------------------------------------------------------------

class TestAgentSnapshot:
    def test_snapshot_immutable(self):
        snap = AgentSnapshot(tick=1, emails=({"subject": "hi"},))
        with pytest.raises(AttributeError):
            snap.tick = 2  # type: ignore[misc]

    def test_snapshot_defaults(self):
        snap = AgentSnapshot()
        assert snap.tick == 0
        assert snap.emails == ()
        assert snap.task_states == {}
        assert snap.shared_kb_snapshot == {}
        assert snap.lock_states == {}
        assert snap.private_workspace_path == ""

    def test_snapshot_equality(self):
        s1 = AgentSnapshot(tick=5, emails=({"a": 1},))
        s2 = AgentSnapshot(tick=5, emails=({"a": 1},))
        assert s1 == s2

    def test_snapshot_inequality(self):
        s1 = AgentSnapshot(tick=1)
        s2 = AgentSnapshot(tick=2)
        assert s1 != s2

    def test_snapshot_emails_are_tuple(self):
        snap = AgentSnapshot(emails=({"subject": "hello"},))
        assert isinstance(snap.emails, tuple)


# ---------------------------------------------------------------------------
# AgentRuntime protocol
# ---------------------------------------------------------------------------

class TestAgentRuntime:
    def test_base_agent_observe(self, tool_registry):
        agent = BaseAgent(agent_id="agent.a", tool_registry=tool_registry)
        snapshot = AgentSnapshot(
            tick=5,
            emails=({"subject": "hello"},),
            task_states={"t1": {"status": "assigned"}},
            shared_kb_snapshot={"paths": ["report.md"]},
            lock_states={},
            private_workspace_path="/private/agent.a",
        )
        obs = agent.observe(snapshot)
        assert obs.agent_id == "agent.a"
        assert obs.tick == 5
        assert len(obs.emails) == 1

    def test_base_agent_decide_empty(self, tool_registry):
        agent = BaseAgent(agent_id="agent.a", tool_registry=tool_registry)
        obs = AgentObservation(agent_id="agent.a", tick=0)
        plan = agent.decide(obs)
        assert plan.agent_id == "agent.a"
        assert len(plan.actions) == 0

    def test_base_agent_act_empty_plan(self, tool_registry):
        agent = BaseAgent(agent_id="agent.a", tool_registry=tool_registry)
        plan = ActionPlan(agent_id="agent.a", tick=0, actions=[])
        ctx = ActionContext(
            agent_id="agent.a",
            tick=0,
            tool_context=ToolContext(agent_id="agent.a"),
        )
        results = agent.act(plan, ctx)
        assert len(results) == 0

    def test_runtime_is_protocol(self, tool_registry):
        agent = BaseAgent(agent_id="agent.a", tool_registry=tool_registry)
        assert isinstance(agent, AgentRuntime)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class TestSimulation:
    def test_create_from_config(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        assert sim.current_tick == 0
        assert len(sim.agent_tree) == 2

    def test_create_from_json_file(self, tmp_path, sample_agent_tree):
        config = {
            "simulation": {"tick_duration_value": 5},
            "agents": [
                {
                    "agent_id": "agent.root",
                    "display_name": "Root",
                    "role": "root_decision_agent",
                    "parent_id": None,
                    "children": ["agent.research"],
                    "tools": ["read", "write", "ls", "delegate"],
                    "can_delegate": True,
                },
                {
                    "agent_id": "agent.research",
                    "display_name": "Research",
                    "role": "research_manager",
                    "parent_id": "agent.root",
                    "children": [],
                    "tools": ["read", "write", "ls"],
                    "can_delegate": False,
                },
            ],
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))

        sim = Simulation.from_config_file(config_file)
        assert sim.config.tick_duration_value == 5

    def test_agents_initialized(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        # Each agent should have a mailbox
        assert sim.mail_system.get_mailbox("agent.root") is not None
        assert sim.mail_system.get_mailbox("agent.research") is not None

    def test_agents_have_runtimes(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        # Runtimes should be created
        assert hasattr(sim, "_runtimes")
        assert len(sim._runtimes) == 2

    def test_root_agent_restricted(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        root_runtime = sim._runtimes["agent.root"]
        assert isinstance(root_runtime, RootAgent)
        assert root_runtime.tool_context.allowed_tools == ROOT_TOOLS

    def test_run_single_tick(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        result = sim.run_tick()
        assert result.tick == 0
        assert sim.current_tick == 1

    def test_run_multiple_ticks(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        results = sim.run(max_ticks=3)
        assert len(results) == 3
        assert sim.current_tick == 3

    def test_audit_log_recorded(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        sim.run_tick()
        # Should have agent creation + tick complete events
        assert sim.audit_log.count > 0

    def test_human_control_accessible(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        result = sim.human_control.view_simulation_status()
        assert result["tick"] == 0
        assert result["agent_count"] == 2

    def test_pause_prevents_advance(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        sim.run_tick()
        sim.human_control.pause()
        results = sim.run(max_ticks=3)
        assert len(results) == 0  # should not advance

    def test_shared_kb_accessible(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        assert sim.shared_kb is not None

    def test_delegation_accessible(self, sample_agent_tree):
        sim = Simulation(agent_tree=sample_agent_tree)
        assert sim.delegation is not None
