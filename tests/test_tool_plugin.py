"""Tool plugin API tests (v0.10 T7).

Verifies:
- register_tool validation: manifest validity, name uniqueness (also
  against builtins), no implicit policy allowlist
- plugin handlers receive injected subsystem handles (context.handles)
  and never touch Simulation internals
- plugin tools callable through the registry AND through a full tick
  (kernel code unchanged)
- LLM tool definitions generated from manifests: all 12 builtins
  renderable, no hand-written table
- deny-by-default: manifest-less tools and un-allowlisted plugin tools
  are denied while a policy is active
"""

from __future__ import annotations

from typing import Any

import pytest

from my_team.agent_runtime import BaseAgent, ToolContext, ToolRegistry
from my_team.agent_tree import AgentTree
from my_team.executor_registry import ExecutorTier
from my_team.models.intent import SubmitToolRequest
from my_team.prompt_templates import PromptTemplates
from my_team.simulation import Simulation
from my_team.tool_manifest import (
    ExecutionClass,
    OperationPolicy,
    ToolManifest,
    ToolManifestError,
    builtin_manifests,
    manifest_to_tool_definition,
)


def _make_tree(tools: list[str] | None = None) -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": tools or ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


def _plugin_manifest(
    name: str = "greet",
    *,
    input_schema: dict[str, Any] | None = None,
    required_inputs: tuple[str, ...] = (),
    execution_class: ExecutionClass = ExecutionClass.READ_ONLY,
) -> ToolManifest:
    return ToolManifest(
        name=name,
        version="1.0.0",
        execution_class=execution_class,
        description=f"Plugin tool {name}",
        input_schema=input_schema or {"msg": {"type": "string"}},
        required_inputs=required_inputs,
    )


class _IntentAgent(BaseAgent):
    """Minimal agent that emits a fixed intent list each tick."""

    def __init__(self, agent_id: str, intents_fn: Any, **kwargs: Any) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self._intents_fn = intents_fn

    def decide_intents(self, observation: Any, continuation: Any = None):
        return self._intents_fn(observation, continuation)


def _grant(sim: Simulation, tools: set[str]) -> None:
    """Register the agent's allowed tools in the registry (per-agent)."""
    sim._tool_registry.register_agent("agent.root", frozenset(tools))


class TestRegisterToolValidation:
    def test_invalid_manifest_raises(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        with pytest.raises(ToolManifestError):
            sim.register_tool(
                ToolManifest(name="bad", version="", execution_class=ExecutionClass.READ_ONLY),
                lambda **_: None,
            )

    def test_duplicate_registration_raises(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.register_tool(_plugin_manifest("greet"), lambda **_: None)
        with pytest.raises(ToolManifestError, match="already registered"):
            sim.register_tool(_plugin_manifest("greet"), lambda **_: None)

    def test_duplicate_against_builtin_raises(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        with pytest.raises(ToolManifestError, match="already registered"):
            sim.register_tool(builtin_manifests()["read"], lambda **_: None)

    def test_registry_level_uniqueness(self) -> None:
        reg = ToolRegistry()
        reg.register_tool(_plugin_manifest("greet"), lambda **_: None)
        with pytest.raises(ToolManifestError, match="already registered"):
            reg.register_tool(_plugin_manifest("greet"), lambda **_: None)

    def test_register_tool_with_executor(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.register_tool(
            ToolManifest(
                name="ext_call",
                version="1.0.0",
                execution_class=ExecutionClass.EXTERNAL_IRREVERSIBLE,
                reversible=False,
            ),
            lambda **_: {"ok": True},
            executor=ExecutorTier.UNTRUSTED_OUT_OF_PROCESS,
        )
        rec = sim._executors.get("ext_call")
        assert rec is not None
        assert rec.tier == ExecutorTier.UNTRUSTED_OUT_OF_PROCESS
        assert sim._tool_registry.get_manifest("ext_call") is not None


class TestPluginHandlerContext:
    def test_subsystem_handles_injected(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        seen: dict[str, Any] = {}

        def handler(context: ToolContext, **_kw: Any) -> Any:
            seen["handles"] = context.handles
            seen["agent_id"] = context.agent_id
            return {"ok": True}

        sim.register_tool(_plugin_manifest("greet"), handler)
        _grant(sim, {"greet"})
        ctx = ToolContext(
            agent_id="agent.root", allowed_tools=frozenset({"greet"}),
        )
        result = sim._tool_registry.execute(ctx, "greet")
        assert result.success
        assert seen["agent_id"] == "agent.root"
        for key in ("private_store", "shared_kb", "mail_system", "task_tree"):
            assert key in seen["handles"], f"missing handle {key}"

    def test_plugin_reads_private_file_via_handle(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        home = sim._private_store.agent_home("agent.root")
        home.mkdir(parents=True, exist_ok=True)
        (home / "notes.txt").write_text("secret", encoding="utf-8")
        seen: dict[str, Any] = {}

        def handler(context: ToolContext, **_kw: Any) -> Any:
            ps = context.handles["private_store"]
            target = ps.resolve_path(context.agent_id, "notes.txt")
            seen["content"] = target.read_text(encoding="utf-8")
            return {"ok": True}

        sim.register_tool(_plugin_manifest("read_note"), handler)
        _grant(sim, {"read_note"})
        ctx = ToolContext(
            agent_id="agent.root", allowed_tools=frozenset({"read_note"}),
        )
        result = sim._tool_registry.execute(ctx, "read_note")
        assert result.success
        assert seen["content"] == "secret"

    def test_handler_uses_no_simulation_state(self) -> None:
        # A plugin handler only sees the context: no access path to
        # Simulation internals exists in its signature.
        sim = Simulation(agent_tree=_make_tree())

        def handler(context: ToolContext, **kwargs: Any) -> Any:
            return {"has_only": sorted(kwargs), "ctx_fields": sorted(context.__dataclass_fields__)}

        sim.register_tool(_plugin_manifest("greet"), handler)
        _grant(sim, {"greet"})
        ctx = ToolContext(agent_id="agent.root", allowed_tools=frozenset({"greet"}))
        result = sim._tool_registry.execute(ctx, "greet", msg="hi")
        assert result.success
        assert result.data["has_only"] == ["msg"]
        assert "handles" in result.data["ctx_fields"]


class TestGeneratedToolDefinitions:
    def test_all_builtins_generated(self) -> None:
        manifests = builtin_manifests()
        assert len(manifests) == 15  # 12 + kb_read/kb_list/kb_search (T8a)
        templates = PromptTemplates()
        tools = templates.render_tool_definitions(
            frozenset(manifests), manifests=manifests,
        )
        assert len(tools) == 15
        assert {t.name for t in tools} == set(manifests)
        for t in tools:
            m = manifests[t.name]
            assert t.description, f"{t.name}: description missing"
            assert t.parameters["type"] == "object"
            assert set(t.parameters["properties"]) == set(m.input_schema)
            if m.required_inputs:
                assert t.parameters["required"] == list(m.required_inputs)

    def test_required_inputs_from_manifest(self) -> None:
        d = manifest_to_tool_definition(builtin_manifests()["read"])
        assert d.parameters["required"] == ["path"]
        assert d.parameters["properties"]["path"]["type"] == "string"
        ls = manifest_to_tool_definition(builtin_manifests()["ls"])
        assert "required" not in ls.parameters

    def test_plugin_tool_definition(self) -> None:
        m = _plugin_manifest(
            "greet",
            input_schema={"name": {"type": "string"}, "tone": {"type": "string"}},
            required_inputs=("name",),
        )
        d = manifest_to_tool_definition(m)
        assert d.name == "greet"
        assert d.description == "Plugin tool greet"
        assert d.parameters["required"] == ["name"]

    def test_manifest_less_tool_yields_no_definition(self) -> None:
        templates = PromptTemplates()
        tools = templates.render_tool_definitions(
            frozenset({"ghost"}), manifests=builtin_manifests(),
        )
        assert tools == []


class TestPolicyDenyByDefault:
    def test_plugin_tool_denied_without_allowlist(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.register_tool(_plugin_manifest("greet"), lambda **_: {"ok": True})
        _grant(sim, {"read", "greet"})
        sim._tool_registry.set_policy(OperationPolicy(allowed=frozenset({"read"})))
        ctx = ToolContext(
            agent_id="agent.root", allowed_tools=frozenset({"read", "greet"}),
        )
        result = sim._tool_registry.execute(ctx, "greet")
        assert not result.success
        assert result.error_code == "policy_denied"

    def test_plugin_tool_allowed_when_allowlisted(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.register_tool(_plugin_manifest("greet"), lambda **_: {"ok": True})
        _grant(sim, {"read", "greet"})
        sim._tool_registry.set_policy(
            OperationPolicy(allowed=frozenset({"read", "greet"})),
        )
        ctx = ToolContext(
            agent_id="agent.root", allowed_tools=frozenset({"read", "greet"}),
        )
        result = sim._tool_registry.execute(ctx, "greet")
        assert result.success

    def test_manifest_less_tool_denied_under_policy(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        # Legacy bare-handler registration without a manifest.
        sim._tool_registry.register_handler("ghost", lambda **_: {"ok": True})
        _grant(sim, {"read", "ghost"})
        sim._tool_registry.set_policy(OperationPolicy(allowed=frozenset({"read"})))
        ctx = ToolContext(
            agent_id="agent.root", allowed_tools=frozenset({"read", "ghost"}),
        )
        result = sim._tool_registry.execute(ctx, "ghost")
        assert not result.success
        assert result.error_code == "policy_denied"
        assert "no manifest" in (result.error or "")

    def test_register_tool_policy_param_attaches_policy(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.register_tool(
            _plugin_manifest("greet"),
            lambda **_: {"ok": True},
            policy=OperationPolicy(allowed=frozenset({"read", "greet"})),
        )
        _grant(sim, {"read", "greet", "write"})
        assert sim._tool_registry.policy is not None
        ctx = ToolContext(
            agent_id="agent.root",
            allowed_tools=frozenset({"read", "greet", "write"}),
        )
        assert sim._tool_registry.execute(ctx, "greet").success
        denied = sim._tool_registry.execute(ctx, "write")
        assert not denied.success
        assert denied.error_code == "policy_denied"


class TestAgentTickIntegration:
    def test_plugin_tool_callable_through_tick(self) -> None:
        sim = Simulation(agent_tree=_make_tree(tools=["read", "greet"]))
        called = {"n": 0}

        def handler(context: ToolContext, msg: str = "", **_kw: Any) -> Any:
            called["n"] += 1
            return {"echo": msg}

        sim.register_tool(_plugin_manifest("greet"), handler)
        agent = _IntentAgent(
            "agent.root",
            intents_fn=lambda obs, cont: [
                SubmitToolRequest(
                    agent_id="agent.root", tool_name="greet",
                    arguments={"msg": "hi"},
                ),
            ],
        )
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        result = sim.run_tick()
        assert result.committed
        assert called["n"] == 1
