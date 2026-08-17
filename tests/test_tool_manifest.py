"""ToolManifest + OperationPolicy tests (v0.7.0 P1-1).

Verifies:
- Manifest registration validation (required fields, execution-class
  coherence, effect-type membership, filesystem scopes)
- OperationPolicy deny-by-default semantics: allowlist, approval,
  network deny, filesystem scope, wall-time cap, output cap,
  irreversibility
- Registry integration: manifests registered with handlers, policy
  gate in execute(), no-manifest tools denied under policy
- Builtin manifests: all v0.6.0 tools registered in Simulation with
  correct execution classes; staged effects match declared effect_types
"""

from __future__ import annotations

import pytest

from my_team.agent_runtime import ToolContext, ToolRegistry
from my_team.agent_tree import AgentTree
from my_team.simulation import Simulation
from my_team.tool_manifest import (
    ExecutionClass,
    OperationPolicy,
    RetryPolicy,
    ToolManifest,
    ToolManifestError,
    builtin_manifests,
)
from my_team.transaction import EffectType


def _manifest(**overrides) -> ToolManifest:
    base: dict = dict(
        name="t",
        version="1.0.0",
        execution_class=ExecutionClass.READ_ONLY,
    )
    base.update(overrides)
    return ToolManifest(**base)


class TestManifestValidation:
    """Registration-time validation rules."""

    def test_required_fields(self) -> None:
        with pytest.raises(ToolManifestError, match="name"):
            _manifest(name="")
        with pytest.raises(ToolManifestError, match="version"):
            _manifest(version="  ")

    def test_execution_class_coherence(self) -> None:
        # STAGED_MUTATION must declare effects
        with pytest.raises(ToolManifestError, match="effect_type"):
            _manifest(execution_class=ExecutionClass.STAGED_MUTATION)
        # READ_ONLY cannot declare effects
        with pytest.raises(ToolManifestError, match="effect_types"):
            _manifest(effect_types=(EffectType.FILE_WRITE,))
        # PURE must be deterministic
        with pytest.raises(ToolManifestError, match="deterministic"):
            _manifest(
                execution_class=ExecutionClass.PURE, deterministic=False,
            )
        # EXTERNAL_IRREVERSIBLE must be declared irreversible
        with pytest.raises(ToolManifestError, match="reversible"):
            _manifest(
                execution_class=ExecutionClass.EXTERNAL_IRREVERSIBLE,
                reversible=True,
            )

    def test_effect_type_membership(self) -> None:
        with pytest.raises(ToolManifestError, match="non-EffectType"):
            _manifest(
                execution_class=ExecutionClass.STAGED_MUTATION,
                effect_types=("file_write",),  # type: ignore[arg-type]
            )

    def test_filesystem_scope_membership(self) -> None:
        with pytest.raises(ToolManifestError, match="filesystem_scope"):
            _manifest(filesystem_scopes=("host-root",))

    def test_nonnegative_limits(self) -> None:
        with pytest.raises(ToolManifestError, match="max_runtime_ms"):
            _manifest(max_runtime_ms=-1)
        with pytest.raises(ToolManifestError, match="max_output_bytes"):
            _manifest(max_output_bytes=-5)

    def test_valid_manifest_ok(self) -> None:
        m = _manifest(
            execution_class=ExecutionClass.STAGED_MUTATION,
            effect_types=(EffectType.FILE_WRITE,),
            filesystem_scopes=("private",),
            max_runtime_ms=1000,
            max_output_bytes=1024,
            retry_policy=RetryPolicy.EXPONENTIAL,
            supports_cancel=True,
        )
        assert m.declares_effect(EffectType.FILE_WRITE)
        assert not m.declares_effect(EffectType.EMAIL_SEND)


class TestOperationPolicy:
    """Deny-by-default policy semantics."""

    def test_deny_by_default(self) -> None:
        policy = OperationPolicy(allowed=frozenset({"read"}))
        decision = policy.decide(_manifest(name="write", effect_types=()))
        assert not decision.allowed
        assert "allowlist" in decision.reason

    def test_allowlist_ok(self) -> None:
        policy = OperationPolicy(allowed=frozenset({"read"}))
        decision = policy.decide(_manifest(name="read"))
        assert decision.allowed and not decision.requires_approval

    def test_requires_approval(self) -> None:
        policy = OperationPolicy(
            allowed=frozenset({"read", "write"}),
            requires_approval=frozenset({"write"}),
        )
        decision = policy.decide(
            _manifest(
                name="write",
                execution_class=ExecutionClass.STAGED_MUTATION,
                effect_types=(EffectType.FILE_WRITE,),
            )
        )
        assert decision.allowed and decision.requires_approval

    def test_approval_must_be_in_allowlist(self) -> None:
        with pytest.raises(ToolManifestError, match="subset of allowed"):
            OperationPolicy(
                allowed=frozenset({"read"}),
                requires_approval=frozenset({"write"}),
            )

    def test_network_denied(self) -> None:
        policy = OperationPolicy(allowed=frozenset({"web"}))
        decision = policy.decide(
            _manifest(name="web", requires_network=True)
        )
        assert not decision.allowed
        assert "network" in decision.reason

    def test_filesystem_scope(self) -> None:
        # Tool scoped to 'system' never allowed under a restricted scope
        policy = OperationPolicy(
            allowed=frozenset({"t"}),
            filesystem_scope="private",
        )
        decision = policy.decide(
            _manifest(filesystem_scopes=("system",))
        )
        assert not decision.allowed
        # Tool scoped to 'private' matches policy scope
        assert policy.decide(
            _manifest(filesystem_scopes=("private",))
        ).allowed
        # Tool with no filesystem access is fine anywhere
        assert policy.decide(
            _manifest(filesystem_scopes=("none",))
        ).allowed

    def test_wall_time_cap(self) -> None:
        policy = OperationPolicy(
            allowed=frozenset({"t"}), max_wall_time_ms=1000,
        )
        decision = policy.decide(_manifest(max_runtime_ms=2000))
        assert not decision.allowed
        assert "max_runtime_ms" in decision.reason
        assert policy.decide(_manifest(max_runtime_ms=500)).allowed

    def test_output_cap(self) -> None:
        policy = OperationPolicy(
            allowed=frozenset({"t"}), max_output_bytes=1024,
        )
        decision = policy.decide(_manifest(max_output_bytes=2048))
        assert not decision.allowed
        assert policy.decide(_manifest(max_output_bytes=512)).allowed

    def test_irreversible_denied(self) -> None:
        policy = OperationPolicy(
            allowed=frozenset({"t"}), reversible=False,
        )
        decision = policy.decide(
            _manifest(
                execution_class=ExecutionClass.EXTERNAL_IRREVERSIBLE,
                reversible=False,
            )
        )
        assert not decision.allowed
        assert "irreversible" in decision.reason


class TestRegistryIntegration:
    """Manifest registration + policy gate in ToolRegistry.execute."""

    def test_register_manifest_and_lookup(self) -> None:
        reg = ToolRegistry()
        m = _manifest(name="read")
        reg.register_manifest(m)
        assert reg.get_manifest("read") is m
        assert reg.manifests() == (m,)

    def test_register_handler_with_manifest(self) -> None:
        reg = ToolRegistry()
        reg.register_handler("read", lambda **_: None, manifest=_manifest(name="read"))
        assert reg.get_manifest("read") is not None

    def test_manifest_name_must_match_tool(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(ValueError, match="does not match"):
            reg.register_handler("read", lambda **_: None, manifest=_manifest(name="ls"))

    def test_invalid_manifest_rejected_at_registration(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(ToolManifestError):
            reg.register_handler(
                "write", lambda **_: None,
                manifest=_manifest(
                    name="write",
                    execution_class=ExecutionClass.STAGED_MUTATION,
                    effect_types=(),
                ),
            )
        assert reg.get_manifest("write") is None

    def test_policy_gate_denies(self) -> None:
        reg = ToolRegistry()
        reg.register_handler(
            "send_email", lambda **_: {"staged": True},
            manifest=builtin_manifests()["send_email"],
        )
        reg.register_agent("a", frozenset({"send_email"}))
        reg.set_policy(OperationPolicy(allowed=frozenset({"read"})))
        ctx = ToolContext(
            agent_id="a", tick=0, allowed_tools=frozenset({"send_email"}),
        )
        result = reg.execute(ctx, "send_email", to=["b"], subject="s")
        assert result.success is False
        assert result.error_code == "policy_denied"

    def test_policy_gate_approval(self) -> None:
        reg = ToolRegistry()
        reg.register_handler(
            "send_email", lambda **_: {"staged": True},
            manifest=builtin_manifests()["send_email"],
        )
        reg.register_agent("a", frozenset({"send_email"}))
        reg.set_policy(OperationPolicy(
            allowed=frozenset({"send_email"}),
            requires_approval=frozenset({"send_email"}),
        ))
        ctx = ToolContext(
            agent_id="a", tick=0, allowed_tools=frozenset({"send_email"}),
        )
        result = reg.execute(ctx, "send_email", to=["b"], subject="s")
        assert result.success is False
        assert result.error_code == "requires_approval"

    def test_no_manifest_denied_under_policy(self) -> None:
        reg = ToolRegistry()
        reg.register_handler("legacy", lambda **_: "ok")
        reg.register_agent("a", frozenset({"legacy"}))
        reg.set_policy(OperationPolicy(allowed=frozenset({"legacy"})))
        ctx = ToolContext(agent_id="a", tick=0, allowed_tools=frozenset({"legacy"}))
        result = reg.execute(ctx, "legacy")
        assert result.success is False
        assert result.error_code == "policy_denied"
        assert "no manifest" in (result.error or "")

    def test_no_policy_legacy_path(self) -> None:
        reg = ToolRegistry()
        reg.register_handler("legacy", lambda **_: "ok")
        reg.register_agent("a", frozenset({"legacy"}))
        ctx = ToolContext(agent_id="a", tick=0, allowed_tools=frozenset({"legacy"}))
        result = reg.execute(ctx, "legacy")
        assert result.success is True


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


class TestBuiltinManifests:
    """Simulation registers all builtin tools with correct manifests."""

    def test_all_builtins_registered(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        names = {m.name for m in sim._tool_registry.manifests()}
        assert names == {
            "read", "ls", "write", "kb_write", "send_email", "delegate",
            "apply_patch", "run_tests", "git_diff", "git_status",
            "python_compute", "python_transform",
        }

    def test_execution_classes(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        reg = sim._tool_registry
        assert reg.get_manifest("read").execution_class is ExecutionClass.READ_ONLY
        assert reg.get_manifest("ls").execution_class is ExecutionClass.READ_ONLY
        assert reg.get_manifest("write").execution_class \
            is ExecutionClass.STAGED_MUTATION
        assert reg.get_manifest("kb_write").execution_class \
            is ExecutionClass.STAGED_MUTATION
        assert reg.get_manifest("send_email").execution_class \
            is ExecutionClass.STAGED_MUTATION
        assert reg.get_manifest("delegate").execution_class \
            is ExecutionClass.STAGED_MUTATION

    def test_staged_effects_match_declared_effect_types(self) -> None:
        """The effects a tool stages must be within its manifest's
        declared effect_types (effect audit)."""
        sim = Simulation(agent_tree=_make_tree())
        ctx = ToolContext(
            agent_id="agent.root", tick=0,
            allowed_tools=frozenset({"delegate"}),
        )
        result = sim._tool_registry.execute(
            ctx, "delegate", recipient_agent_id="agent.research",
            task_title="T", task_description="",
        )
        assert result.success
        effects = sim._transaction_buffer.get_effects("agent.root")
        manifest = sim._tool_registry.get_manifest("delegate")
        assert effects, "delegate must stage effects"
        for effect in effects:
            assert manifest.declares_effect(effect.effect_type), (
                f"staged {effect.effect_type.value} not in manifest "
                f"effect_types {[e.value for e in manifest.effect_types]}"
            )
        types = {e.effect_type for e in effects}
        assert types == {EffectType.TASK_CREATE, EffectType.EMAIL_SEND}

    def test_policy_enforced_in_simulation(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim._tool_registry.set_policy(
            OperationPolicy(allowed=frozenset({"read", "write", "ls", "delegate"}))
        )
        # send_email not in allowlist → denied even though the agent
        # has the tool permission
        ctx = ToolContext(
            agent_id="agent.research", tick=0,
            allowed_tools=frozenset({"send_email"}),
        )
        result = sim._tool_registry.execute(
            ctx, "send_email", to=["agent.root"], subject="s", body="",
        )
        assert result.success is False
        assert result.error_code == "policy_denied"
        # read (allowed) passes the policy gate
        read_ctx = ToolContext(
            agent_id="agent.root", tick=0,
            allowed_tools=frozenset({"read"}),
        )
        assert sim._tool_registry.execute(
            read_ctx, "read", path="missing.txt",
        ).error_code in {None, "tool_error"}
