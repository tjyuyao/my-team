"""Frozen snapshot read view + file-write rollback (v0.6.0 review §四.3).

- read/ls execute against the per-agent file view captured at Freeze;
  writes staged in the same tick are invisible until Commit
- next-tick reads see the committed file
- a FILE_WRITE applied in a tick that later rolls back is undone
  (previous content restored / newly created file removed)
"""

from __future__ import annotations

from my_team.agent_runtime import ActionResult, AgentAction
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.intent import Intent, SubmitToolRequest
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
                "children": [],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


def _read_plan(path: str) -> dict[str, list[Intent]]:
    return {"agent.root": [SubmitToolRequest(
        agent_id="agent.root",
        tool_name="read",
        arguments={"path": path},
    )]}


def _read_validated() -> dict[str, list[ActionResult]]:
    return {"agent.root": [ActionResult(
        action=AgentAction(
            action_type="submit_tool_request",
            tool_name="read",
            payload={"path": ""},
        ),
        success=True,
        result_data={"validated": True},
    )]}


class TestFrozenReadView:
    def test_same_tick_reads_use_frozen_snapshot(self) -> None:
        """A read in the same tick does NOT see a write staged after
        Freeze — reads execute against the frozen snapshot view."""
        sim = Simulation(agent_tree=_make_tree())
        target = sim._private_store.agent_home("agent.root") / "f.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("v1", encoding="utf-8")

        # Freeze: snapshot the state (contains v1)
        snapshot = sim._build_snapshot(0)
        view = snapshot["private_files"]["agent.root"]
        assert view["files"]["f.md"] == "v1"

        # Another actor stages a write of v2 in the same tick (after Freeze)
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE,
            "agent.other",
            "f.md",
            data={"content": "v2"},
        )

        # A read executed in Act against the frozen view returns v1
        results = sim._phase_act(
            0, _read_plan("f.md"), ready=[], validated=_read_validated(),
            snapshot=snapshot,
        )
        assert results["agent.root"][0].success
        assert results["agent.root"][0].result_data["content"] == "v1"

    def test_next_tick_reads_see_committed_file(self) -> None:
        """A read in the next tick sees the committed write."""
        sim = Simulation(agent_tree=_make_tree())

        # Commit v2 via the pipeline
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE,
            "agent.root",
            "f.md",
            data={"content": "v2"},
        )
        sim._phase_commit(0, {})

        # Next tick's frozen view contains v2
        snapshot = sim._build_snapshot(1)
        assert snapshot["private_files"]["agent.root"]["files"]["f.md"] == "v2"
        results = sim._phase_act(
            1, _read_plan("f.md"), ready=[], validated=_read_validated(),
            snapshot=snapshot,
        )
        assert results["agent.root"][0].result_data["content"] == "v2"


class TestFileWriteRollback:
    def _write_and_fail(self, sim: Simulation, path: str) -> None:
        """Stage FILE_WRITE(path) then a failing TASK_CREATE (duplicate id)."""
        sim.task_tree.create(
            task_id="task.conflict",
            title="Existing",
            creator_agent_id="agent.root",
            owner_agent_id="agent.root",
        )
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE,
            "agent.root",
            path,
            data={"content": "new content"},
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE,
            "agent.root",
            "task.conflict",  # duplicate → raises
            data={
                "task_id": "task.conflict",
                "title": "Dup",
                "creator_agent_id": "agent.root",
                "owner_agent_id": "agent.root",
            },
        )

    def test_file_write_rollback_restores_previous_content(self) -> None:
        """If a later effect fails, an applied FILE_WRITE is undone and
        the previous content is restored."""
        from uuid import uuid4
        path = f"notes-{uuid4().hex[:8]}.md"
        sim = Simulation(agent_tree=_make_tree())
        target = sim._private_store.agent_home("agent.root") / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original", encoding="utf-8")

        self._write_and_fail(sim, path)
        sim._phase_commit(0, {})

        assert target.read_text(encoding="utf-8") == "original"
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) >= 1

    def test_file_write_rollback_removes_new_file(self) -> None:
        """A file created (not overwritten) in the tick is deleted on rollback."""
        from uuid import uuid4
        path = f"notes-{uuid4().hex[:8]}.md"
        sim = Simulation(agent_tree=_make_tree())
        target = sim._private_store.agent_home("agent.root") / path
        assert not target.exists()

        self._write_and_fail(sim, path)
        sim._phase_commit(0, {})

        assert not target.exists()
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) >= 1
