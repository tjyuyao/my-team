"""Tests for AgentRuntime, ToolContext, and Simulation integration.

Covers review gaps §8.1 (Simulation), §8.2 (AgentRuntime), §8.3 (identity).

v0.11（N1b，§5.1）：白名单断言全部迁移为**两层 Grant 求值断言**——
测试内显式布线：建 Authority → 注册工具 uuid（capability）→
grant_membership + grant_capability（§3.5：∃position：Grant(agent,
position) ∧ Grant(position, entity_id)）。
"""

import json

import pytest

from my_team.agent_runtime import (
    ActionContext,
    ActionPlan,
    AgentObservation,
    AgentRuntime,
    AgentSnapshot,
    BaseAgent,
    ManagerAgent,
    RootAgent,
    SubAgent,
    ToolContext,
    ToolPermissionError,
    ToolRegistry,
    ToolResult,
)
from my_team.agent_tree import AgentTree
from my_team.devices.authority import Authority, new_team_id
from my_team.simulation import Simulation
from my_team.tool_manifest import builtin_manifests

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
    """N1b 两层 Grant 布线：Authority → 注册工具 uuid → 授予。

    直派形态：agent 以自身为 position（grant_membership(agent_id,
    agent_id)）；工具 capability 授予该 position（grant_capability）。
    """
    authority = Authority(team_id=new_team_id(), owner_agent_id="agent.root")
    reg = ToolRegistry(authority=authority)
    for manifest in builtin_manifests().values():
        reg.register_manifest(manifest)
    reg.declare_tools(
        "agent.root",
        frozenset({"read", "write", "ls", "delegate"}),
    )
    reg.declare_tools(
        "agent.research",
        frozenset({"read", "write", "ls", "delegate", "send_email"}),
    )
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
# ToolRegistry — 两层 Grant 求值（N1b，§5.1）
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_authorize_allowed(self, tool_registry):
        ctx = ToolContext(agent_id="agent.root")
        tool_registry.authorize(ctx, "read")  # should not raise

    def test_authorize_denied(self, tool_registry):
        ctx = ToolContext(agent_id="agent.root")
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
        ctx = ToolContext(agent_id="agent.root")
        result = tool_registry.execute(ctx, "read", path="test.md")
        assert result.success
        assert result.data == "content of test.md"

    def test_execute_no_handler(self, tool_registry):
        ctx = ToolContext(agent_id="agent.root")
        # "nonexistent_tool" 无受控 uuid 且无授予 → 权限拒绝（deny-by-default）
        result = tool_registry.execute(ctx, "nonexistent_tool")
        assert not result.success
        assert "does not have permission" in result.error

    def test_execute_permission_denied(self, tool_registry):
        def handler(context: ToolContext) -> ToolResult:
            return ToolResult(success=True)

        tool_registry.register_handler("send_email", handler)
        ctx = ToolContext(agent_id="agent.root")
        result = tool_registry.execute(ctx, "send_email")
        assert not result.success
        assert "does not have permission" in result.error


# ---------------------------------------------------------------------------
# Root Agent restrictions — 由两层 Grant 布线决定（N1b，§3.5）
# ---------------------------------------------------------------------------

class TestRootAgentRestrictions:
    def test_root_tools_limited(self, tool_registry):
        RootAgent(agent_id="agent.root", tool_registry=tool_registry)
        assert tool_registry.authorized_tools("agent.root") == frozenset(
            {"read", "write", "ls", "delegate"},
        )

    def test_root_cannot_send_email(self, tool_registry):
        RootAgent(agent_id="agent.root", tool_registry=tool_registry)
        assert not tool_registry.can_use("agent.root", "send_email")

    def test_root_can_read_write_ls_delegate(self, tool_registry):
        RootAgent(agent_id="agent.root", tool_registry=tool_registry)
        for tool in ["read", "write", "ls", "delegate"]:
            assert tool_registry.can_use("agent.root", tool)

    def test_manager_has_more_tools(self, tool_registry):
        ManagerAgent(agent_id="agent.research", tool_registry=tool_registry)
        assert tool_registry.can_use("agent.research", "send_email")
        assert tool_registry.can_use("agent.research", "delegate")

    def test_unwired_worker_denied_by_default(self, tool_registry):
        """未布线的 agent 无任何授予 → deny-by-default（§3.5）。"""
        SubAgent(agent_id="agent.web", tool_registry=tool_registry)
        assert tool_registry.authorized_tools("agent.web") == frozenset()
        assert not tool_registry.can_use("agent.web", "read")

    def test_worker_extra_tools_via_grant(self, tool_registry):
        """业务工具经 grant_capability 授予（不再按 role 内置，§5.1）。"""
        SubAgent(agent_id="agent.web", tool_registry=tool_registry)
        tool_registry.declare_tools(
            "agent.web", frozenset({"read", "web_search"}),
        )
        assert tool_registry.can_use("agent.web", "read")
        # web_search 无 manifest（未注册 uuid）→ 不可授予、不可用
        assert not tool_registry.can_use("agent.web", "web_search")


# ---------------------------------------------------------------------------
# AgentSnapshot
# ---------------------------------------------------------------------------

class TestAgentSnapshot:
    def test_snapshot_immutable(self):
        snap = AgentSnapshot(tick=1, emails=({"subject": "hi"},))
        with pytest.raises(AttributeError):
            snap.tick = 2  # type: ignore[misc]

    def test_snapshot_deeply_immutable(self):
        """Nested dicts cannot be mutated through the snapshot."""
        snap = AgentSnapshot(
            tick=1,
            task_states={"t1": {"status": "assigned"}},
            shared_kb_snapshot={"paths": ["report.md"]},
        )
        # Outer is frozen dataclass — can't reassign
        with pytest.raises(AttributeError):
            snap.task_states = {}  # type: ignore[misc]
        # Inner MappingProxyType — can't mutate values
        with pytest.raises(TypeError):
            snap.task_states["t1"]["status"] = "hacked"  # type: ignore[index]
        with pytest.raises(TypeError):
            snap.shared_kb_snapshot["new_key"] = "value"  # type: ignore[index]

    def test_snapshot_defaults(self):
        snap = AgentSnapshot()
        assert snap.tick == 0
        assert snap.emails == ()
        assert len(snap.task_states) == 0
        assert len(snap.shared_kb_snapshot) == 0
        assert len(snap.lock_states) == 0
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

    def test_snapshot_deeply_immutable_extended(self):
        """Verify immutability: outer MappingProxyType prevents key mutation."""
        snap = AgentSnapshot(
            tick=1,
            task_states={"t1": {"nested": {"deep": "value"}}},
            shared_kb_snapshot={"project": {"items": [1, 2, 3]}},
        )
        # Level 1: can't add/remove keys at top level
        with pytest.raises(TypeError):
            snap.task_states["new_task"] = {}  # type: ignore[index]
        with pytest.raises(TypeError):
            del snap.task_states["t1"]  # type: ignore[index]
        # Level 0: can't reassign field
        with pytest.raises(AttributeError):
            snap.task_states = {}  # type: ignore[misc]
        # Note: deeply nested dict values are NOT recursively frozen.
        # This is a known limitation — see report §P1-2.

    def test_snapshot_lock_states_are_mapping(self):
        from types import MappingProxyType
        snap = AgentSnapshot(
            lock_states={"res/a": {"owner": "agent.a", "lease_until": 10}}
        )
        assert isinstance(snap.lock_states, MappingProxyType)
        # Can't add/remove keys
        with pytest.raises(TypeError):
            snap.lock_states["new_res"] = {}  # type: ignore[index]
        # Can't reassign field
        with pytest.raises(AttributeError):
            snap.lock_states = {}  # type: ignore[misc]

    def test_snapshot_emails_tuple_of_dicts(self):
        """Emails is a tuple; inner dicts remain mutable (by design)."""
        snap = AgentSnapshot(emails=({"subject": "hello"},))
        assert isinstance(snap.emails, tuple)
        # Inner dicts are NOT wrapped (tuple elements aren't auto-frozen)
        # This is a known limitation documented in the report

    def test_snapshot_task_states_are_mapping(self):
        from types import MappingProxyType
        snap = AgentSnapshot(task_states={"t1": {"s": 1}})
        assert isinstance(snap.task_states, MappingProxyType)


# ---------------------------------------------------------------------------
# AgentRuntime protocol
# ---------------------------------------------------------------------------

class TestAgentRuntime:
    def test_base_agent_observe(self, tool_registry):
        from types import MappingProxyType
        agent = BaseAgent(agent_id="agent.a", tool_registry=tool_registry)
        snapshot = AgentSnapshot(
            tick=5,
            emails=({"subject": "hello"},),
            task_states=MappingProxyType({"t1": MappingProxyType({"status": "assigned"})}),
            shared_kb_snapshot=MappingProxyType({"paths": ["report.md"]}),
            lock_states=MappingProxyType({}),
            private_workspace_path="/private/agent.a",
        )
        obs = agent.observe(snapshot)
        assert obs.agent_id == "agent.a"
        assert obs.tick == 5
        assert len(obs.emails) == 1
        # AgentObservation converts to plain dicts
        assert obs.task_states == {"t1": {"status": "assigned"}}

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
        """N1b：root 的可用工具由初始授予集（两层 Grant）决定，不再按
        role 内置白名单（§5.1）。"""
        sim = Simulation(agent_tree=sample_agent_tree)
        root_runtime = sim._runtimes["agent.root"]
        assert isinstance(root_runtime, RootAgent)
        assert sim._tool_registry.authorized_tools("agent.root") == frozenset(
            {"read", "write", "ls", "delegate"},
        )
        assert not sim._tool_registry.can_use("agent.root", "send_email")

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
