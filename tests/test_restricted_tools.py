"""Restricted tools tests (v0.7.0 P1-3).

Verifies:
- patch_ops: strict unified-diff engine (format validation, conflict
  detection, new-file / deletion patches, overlapping hunks)
- apply_patch: STAGED_MUTATION — stages FILE_PATCH at Act, applies at
  Commit, rolls back via file_previous; conflict/invalid → error codes
- sandbox_tools: subprocess timeout (process-group kill), output
  truncation, exit codes
- run_tests / git_diff / git_status: sandboxed-process handlers with
  manifests; workspace-scoped policy enforcement
"""

from __future__ import annotations

import sys
from uuid import uuid4

import pytest

from my_team.agent_runtime import ToolContext
from my_team.agent_tree import AgentTree
from my_team.patch_ops import PatchError, apply_patch, parse_unified_patch
from my_team.sandbox_tools import run_sandboxed_process
from my_team.simulation import Simulation
from my_team.tool_manifest import ExecutionClass, OperationPolicy
from my_team.transaction import EffectStatus, EffectType


class TestPatchOps:
    """Unified-diff engine: format + conflict semantics."""

    def test_apply_edit(self) -> None:
        patch = (
            "--- a/notes.md\n+++ b/notes.md\n"
            "@@ -1,2 +1,3 @@\n"
            " hello\n"
            "-world\n"
            "+world!\n"
            "+new line\n"
        )
        assert apply_patch("hello\nworld", patch) == "hello\nworld!\nnew line"

    def test_conflict_detected(self) -> None:
        patch = (
            "@@ -1,1 +1,1 @@\n"
            "-hello\n"
            "+goodbye\n"
        )
        with pytest.raises(PatchError) as ei:
            apply_patch("different content", patch)
        assert ei.value.conflict is True
        assert "conflict" in str(ei.value)

    def test_malformed_header(self) -> None:
        with pytest.raises(PatchError) as ei:
            parse_unified_patch("@@ -1 +1 @@\n")  # missing count pair
        assert ei.value.conflict is False

    def test_empty_patch_rejected(self) -> None:
        with pytest.raises(PatchError, match="empty"):
            apply_patch("hello", "")

    def test_no_hunks_rejected(self) -> None:
        with pytest.raises(PatchError, match="no hunks"):
            apply_patch("hello", "--- a/x\n+++ b/x\n")

    def test_new_file_creation(self) -> None:
        patch = (
            "--- /dev/null\n+++ b/new.md\n"
            "@@ -0,0 +1,2 @@\n"
            "+line one\n"
            "+line two\n"
        )
        assert apply_patch("", patch) == "line one\nline two"

    def test_new_file_against_existing_content_conflicts(self) -> None:
        patch = (
            "--- /dev/null\n+++ b/new.md\n"
            "@@ -0,0 +1,1 @@\n"
            "+line\n"
        )
        with pytest.raises(PatchError) as ei:
            apply_patch("already here", patch)
        assert ei.value.conflict is True

    def test_delete_file(self) -> None:
        patch = (
            "--- a/old.md\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-line one\n"
            "-line two\n"
        )
        assert apply_patch("line one\nline two", patch) == ""

    def test_overlapping_hunks_rejected(self) -> None:
        patch = (
            "@@ -1,1 +1,1 @@\n"
            "-a\n"
            "+A\n"
            "@@ -1,1 +1,1 @@\n"
            "-b\n"
            "+B\n"
        )
        with pytest.raises(PatchError, match="overlap"):
            apply_patch("a\nb", patch)

    def test_edit_only(self) -> None:
        """Patch that only inserts lines."""
        patch = (
            "@@ -1,1 +1,2 @@\n"
            " base\n"
            "+inserted\n"
        )
        assert apply_patch("base", patch) == "base\ninserted"


def _make_tree(tools: list[str]) -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": tools,
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


def _ctx(sim: Simulation, tool: str) -> ToolContext:
    return ToolContext(
        agent_id="agent.root", tick=0,
        allowed_tools=sim._tool_registry.get_allowed_tools("agent.root"),
    )


class TestApplyPatchTool:
    """apply_patch: staged mutation with conflict detection."""

    def _sim(self) -> Simulation:
        return Simulation(agent_tree=_make_tree(
            ["read", "write", "apply_patch"],
        ))

    def _patch(self, path: str) -> str:
        return (
            "--- a/" + path + "\n+++ b/" + path + "\n"
            "@@ -1,1 +1,1 @@\n"
            "-hello\n"
            "+goodbye\n"
        )

    def test_valid_patch_stages_and_commits(self) -> None:
        path = f"f-{uuid4().hex[:8]}.md"
        sim = self._sim()
        target = sim._private_store.agent_home("agent.root") / path
        target.write_text("hello", encoding="utf-8")

        result = sim._tool_registry.execute(
            _ctx(sim, "apply_patch"), "apply_patch",
            path=path, patch=self._patch(path),
        )
        assert result.success
        # Staged as FILE_PATCH, NOT applied yet
        effects = [
            e for e in sim._transaction_buffer.get_effects("agent.root")
            if e.effect_type == EffectType.FILE_PATCH
        ]
        assert len(effects) == 1
        assert target.read_text(encoding="utf-8") == "hello"
        # Commit applies
        sim._phase_commit(0, {"agent.root": []})
        assert target.read_text(encoding="utf-8") == "goodbye"
        assert effects[0].status == EffectStatus.COMMITTED

    def test_conflict_rejected_nothing_staged(self) -> None:
        path = f"f-{uuid4().hex[:8]}.md"
        sim = self._sim()
        target = sim._private_store.agent_home("agent.root") / path
        target.write_text("completely different", encoding="utf-8")

        result = sim._tool_registry.execute(
            _ctx(sim, "apply_patch"), "apply_patch",
            path=path, patch=self._patch(path),
        )
        assert not result.success
        assert result.error_code == "patch_conflict"
        assert "conflict" in (result.error or "")
        assert sim._transaction_buffer.get_effects("agent.root") == []

    def test_invalid_patch_rejected(self) -> None:
        sim = self._sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "apply_patch"), "apply_patch",
            path="x.md", patch="@@ -1 +1 @@\n",
        )
        assert not result.success
        assert result.error_code == "invalid_patch"

    def test_new_file_patch(self) -> None:
        path = f"new-{uuid4().hex[:8]}.md"
        sim = self._sim()
        patch = (
            "--- /dev/null\n+++ b/" + path + "\n"
            "@@ -0,0 +1,1 @@\n"
            "+fresh\n"
        )
        result = sim._tool_registry.execute(
            _ctx(sim, "apply_patch"), "apply_patch",
            path=path, patch=patch,
        )
        assert result.success
        sim._phase_commit(0, {"agent.root": []})
        target = sim._private_store.agent_home("agent.root") / path
        assert target.read_text(encoding="utf-8") == "fresh"

    def test_rollback_restores_original(self) -> None:
        """A FILE_PATCH applied at Commit is undone if a later effect
        fails (same file_previous mechanism as FILE_WRITE)."""
        path = f"rb-{uuid4().hex[:8]}.md"
        sim = self._sim()
        target = sim._private_store.agent_home("agent.root") / path
        target.write_text("hello", encoding="utf-8")

        sim._tool_registry.execute(
            _ctx(sim, "apply_patch"), "apply_patch",
            path=path, patch=self._patch(path),
        )
        # Duplicate task id → apply failure → full rollback
        sim.task_tree.create(
            task_id="task.conflict", title="Existing",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE, "agent.root", "task.conflict",
            data={
                "task_id": "task.conflict",
                "title": "Dup",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.root",
            },
        )
        sim._phase_commit(0, {"agent.root": []})
        # File restored to pre-tick content
        assert target.read_text(encoding="utf-8") == "hello"
        assert sim.state_epoch == 1

    def test_manifest_declares_file_patch(self) -> None:
        sim = self._sim()
        manifest = sim._tool_registry.get_manifest("apply_patch")
        assert manifest is not None
        assert manifest.execution_class is ExecutionClass.STAGED_MUTATION
        assert manifest.declares_effect(EffectType.FILE_PATCH)
        assert manifest.filesystem_scopes == ("private",)


class TestSandboxedProcess:
    """Timeout (process-group kill), truncation, exit codes."""

    def test_success_and_exit_code(self) -> None:
        res = run_sandboxed_process(
            [sys.executable, "-c", "print('ok'); raise SystemExit(3)"],
        )
        assert res["success"] is False
        assert res["exit_code"] == 3
        assert "ok" in res["stdout"]

    def test_timeout_kills_process_group(self) -> None:
        res = run_sandboxed_process(
            ["sleep", "2"],
            timeout_ms=200,
        )
        assert res["timed_out"] is True
        assert res["success"] is False
        assert res["exit_code"] is None

    def test_output_truncation(self) -> None:
        res = run_sandboxed_process(
            [sys.executable, "-c", "print('x' * 100_000)"],
            max_output_bytes=1000,
        )
        assert len(res["stdout"]) <= 1000 + 200  # marker
        assert "truncated" in res["stdout"]

    def test_rejects_non_list_command(self) -> None:
        with pytest.raises(ValueError):
            run_sandboxed_process([])
        with pytest.raises(ValueError):
            run_sandboxed_process("ls; rm -rf /")  # str, not a list

    def test_no_shell_interpretation(self) -> None:
        """Shell metacharacters in args are LITERAL — no shell=True."""
        res = run_sandboxed_process(
            ["ls; echo pwned > /tmp/x", "&&", "true"],
        )
        # The 'binary' does not exist → spawn failure, never a shell
        assert res["success"] is False
        assert "spawn failed" in res["stderr"]


class TestRunTestsTool:
    """run_tests: sandboxed pytest with real subprocess."""

    def _sim(self) -> Simulation:
        return Simulation(agent_tree=_make_tree(["run_tests"]))

    def test_passing_test_file(self, tmp_path) -> None:
        test_file = tmp_path / "test_ok.py"
        test_file.write_text(
            "def test_true():\n    assert True\n", encoding="utf-8",
        )
        sim = self._sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "run_tests"), "run_tests",
            test_path=str(test_file),
        )
        assert result.success, result.error
        assert result.data["exit_code"] == 0
        assert "1 passed" in result.data["stdout"]

    def test_failing_test_file(self, tmp_path) -> None:
        test_file = tmp_path / "test_fail.py"
        test_file.write_text(
            "def test_false():\n    assert False\n", encoding="utf-8",
        )
        sim = self._sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "run_tests"), "run_tests",
            test_path=str(test_file),
        )
        assert not result.success
        assert result.data["exit_code"] == 1

    def test_manifest_declares_sandboxed_process(self) -> None:
        sim = self._sim()
        manifest = sim._tool_registry.get_manifest("run_tests")
        assert manifest is not None
        assert manifest.execution_class is ExecutionClass.SANDBOXED_PROCESS
        assert manifest.max_runtime_ms is not None
        assert manifest.max_output_bytes is not None


class TestGitTools:
    """git_diff / git_status: read-only sandboxed git on the workspace."""

    def _sim(self) -> Simulation:
        return Simulation(agent_tree=_make_tree(["git_diff", "git_status"]))

    def test_git_status_runs(self) -> None:
        sim = self._sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "git_status"), "git_status",
        )
        assert result.success, result.error
        assert result.data["exit_code"] == 0
        assert isinstance(result.data["stdout"], str)

    def test_git_diff_runs(self) -> None:
        sim = self._sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "git_diff"), "git_diff",
        )
        assert result.success, result.error
        assert result.data["exit_code"] == 0

    def test_manifests_read_only_workspace(self) -> None:
        sim = self._sim()
        for name in ("git_diff", "git_status"):
            manifest = sim._tool_registry.get_manifest(name)
            assert manifest is not None
            assert manifest.execution_class is ExecutionClass.READ_ONLY
            assert manifest.filesystem_scopes == ("workspace",)

    def test_workspace_tools_denied_under_private_policy(self) -> None:
        sim = self._sim()
        sim._tool_registry.set_policy(OperationPolicy(
            allowed=frozenset({"git_diff", "git_status", "run_tests"}),
            filesystem_scope="private",
        ))
        result = sim._tool_registry.execute(
            _ctx(sim, "git_status"), "git_status",
        )
        assert not result.success
        assert result.error_code == "policy_denied"
        assert "filesystem" in (result.error or "")


class TestBuiltinRegistration:
    def test_ten_builtin_manifests(self) -> None:
        sim = Simulation(agent_tree=_make_tree(
            ["read", "write", "ls", "delegate", "apply_patch",
             "run_tests", "git_diff", "git_status"],
        ))
        names = {m.name for m in sim._tool_registry.manifests()}
        assert names == {
            "read", "ls", "write", "kb_write", "send_email", "delegate",
            "apply_patch", "run_tests", "git_diff", "git_status",
        }
