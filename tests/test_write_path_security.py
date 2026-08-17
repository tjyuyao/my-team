"""Main-path tests for FILE_WRITE/FILE_PATCH path traversal fix (P0-1).

Verifies that the write tool, WritePrivateFileIntent, and _phase_commit
all reject paths that attempt to escape the agent's private workspace.
Tests go through Simulation's main path, not just isolated FileOps.

Date: 2026-08-18
"""

from __future__ import annotations

from my_team.agent_runtime import ActionResult, AgentAction, ToolContext
from my_team.agent_tree import AgentTree
from my_team.models.activation import ReadyCandidate
from my_team.models.intent import WritePrivateFileIntent
from my_team.simulation import Simulation
from my_team.transaction import EffectType


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.b"],
                "tools": ["read", "write", "ls", "apply_patch"],
                "can_delegate": False,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.b",
                "display_name": "B",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _make_sim() -> Simulation:
    return Simulation(agent_tree=_make_tree())


def _ok_validated(intent: WritePrivateFileIntent) -> ActionResult:
    return ActionResult(
        action=AgentAction(
            action_type=intent.intent_type.value,
            tool_name="",
            payload=dict(intent.payload),
        ),
        success=True,
        result_data={"validated": True},
    )


class TestCommitPathTraversal:
    """_phase_commit must reject traversal via resolve_path."""

    def test_dotdot_path_denied_in_commit(self) -> None:
        """Committing a FILE_WRITE with ../path must fail and create no file."""
        sim = _make_sim()
        sim._transaction_buffer.stage(
            effect_type=EffectType.FILE_WRITE,
            agent_id="agent.root",
            resource="../agent.b/workspace/pwned.txt",
            data={"content": "pwned"},
        )
        sim._phase_commit(0, {})

        # No file must be created in agent.b's workspace
        b_home = sim._private_store.agent_home("agent.b")
        assert not (b_home / "workspace" / "pwned.txt").exists()
        # The staged effect must be marked failed
        effects = sim._transaction_buffer.get_effects()
        assert any(e.status.value == "failed" for e in effects)

    def test_absolute_path_denied_in_commit(self) -> None:
        """Committing a FILE_WRITE with /tmp path must fail."""
        sim = _make_sim()
        sim._transaction_buffer.stage(
            effect_type=EffectType.FILE_WRITE,
            agent_id="agent.root",
            resource="/tmp/pwned.txt",
            data={"content": "pwned"},
        )
        sim._phase_commit(0, {})

        from pathlib import Path
        assert not Path("/tmp/pwned.txt").exists()
        effects = sim._transaction_buffer.get_effects()
        assert any(e.status.value == "failed" for e in effects)

    def test_valid_path_allowed_in_commit(self) -> None:
        """Committing a FILE_WRITE with a valid relative path succeeds."""
        sim = _make_sim()
        sim._transaction_buffer.stage(
            effect_type=EffectType.FILE_WRITE,
            agent_id="agent.root",
            resource="workspace/hello.txt",
            data={"content": "hello"},
        )
        sim._phase_commit(0, {})

        target = sim._private_store.agent_home("agent.root") / "workspace/hello.txt"
        assert target.read_text() == "hello"


class TestWriteToolPathTraversal:
    """The 'write' tool handler must reject traversal before staging."""

    def test_dotdot_path_rejected_by_write_tool(self) -> None:
        """Write tool with ../path returns error, stages nothing."""
        sim = _make_sim()
        ctx = ToolContext(agent_id="agent.root", tick=0)
        result = sim._tool_registry.execute(
            ctx, "write",
            path="../agent.b/workspace/pwned.txt", content="pwned",
        )
        assert not result.success
        assert "traversal" in result.error
        # No FILE_WRITE effect should be staged
        effects = sim._transaction_buffer.get_effects()
        assert not any(
            e.effect_type == EffectType.FILE_WRITE for e in effects
        )

    def test_absolute_path_rejected_by_write_tool(self) -> None:
        """Write tool with /tmp path returns error."""
        sim = _make_sim()
        ctx = ToolContext(agent_id="agent.root", tick=0)
        result = sim._tool_registry.execute(
            ctx, "write", path="/tmp/evil.txt", content="x",
        )
        assert not result.success
        assert "absolute" in result.error

    def test_empty_path_rejected_by_write_tool(self) -> None:
        """Write tool with empty path returns error."""
        sim = _make_sim()
        ctx = ToolContext(agent_id="agent.root", tick=0)
        result = sim._tool_registry.execute(
            ctx, "write", path="", content="x",
        )
        assert not result.success
        assert "empty" in result.error


class TestWriteIntentPathTraversal:
    """WritePrivateFileIntent must reject traversal before staging."""

    def test_dotdot_intent_rejected(self) -> None:
        """WritePrivateFileIntent with ../path fails in _phase_act."""
        sim = _make_sim()
        intent = WritePrivateFileIntent(
            agent_id="agent.root",
            path="../agent.b/workspace/pwned.txt",
            content="pwned",
        )
        plan: dict[str, list[WritePrivateFileIntent]] = {
            "agent.root": [intent],
        }
        validated = {"agent.root": [_ok_validated(intent)]}
        result = sim._phase_act(
            0, plan,
            ready=[ReadyCandidate(agent_id="agent.root", events=(), tick=0)],
            validated=validated,
        )
        assert not result["agent.root"][0].success
        assert "traversal" in result["agent.root"][0].error

    def test_absolute_intent_rejected(self) -> None:
        """WritePrivateFileIntent with /tmp path fails."""
        sim = _make_sim()
        intent = WritePrivateFileIntent(
            agent_id="agent.root",
            path="/tmp/evil.txt",
            content="x",
        )
        plan: dict[str, list[WritePrivateFileIntent]] = {
            "agent.root": [intent],
        }
        validated = {"agent.root": [_ok_validated(intent)]}
        result = sim._phase_act(
            0, plan,
            ready=[ReadyCandidate(agent_id="agent.root", events=(), tick=0)],
            validated=validated,
        )
        assert not result["agent.root"][0].success
        assert "absolute" in result["agent.root"][0].error


class TestApplyPatchPathTraversal:
    """apply_patch tool must reject traversal paths."""

    def test_dotdot_apply_patch_rejected(self) -> None:
        sim = _make_sim()
        ctx = ToolContext(agent_id="agent.root", tick=0)
        result = sim._tool_registry.execute(
            ctx, "apply_patch",
            path="../agent.b/workspace/pwned.txt",
            patch="--- a\n+++ b\n@@ -0 +1 @@\n+line",
        )
        assert not result.success
        assert "traversal" in result.error


class TestReadPathTraversal:
    """The 'read' tool handler must reject traversal via resolve_path."""

    def test_dotdot_read_denied(self) -> None:
        sim = _make_sim()
        ctx = ToolContext(agent_id="agent.root", tick=0)
        result = sim._tool_registry.execute(
            ctx, "read", path="../agent.b/workspace/secret.txt",
        )
        assert not result.success
        assert "denied" in result.error.lower()

    def test_absolute_read_denied(self) -> None:
        sim = _make_sim()
        ctx = ToolContext(agent_id="agent.root", tick=0)
        result = sim._tool_registry.execute(
            ctx, "read", path="/etc/passwd",
        )
        assert not result.success
        assert "denied" in result.error.lower()
