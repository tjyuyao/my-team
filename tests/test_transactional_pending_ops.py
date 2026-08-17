"""Main-path tests for P0-2: pending op transaction alignment.

Verifies that when _phase_commit rolls back, this-tick registered
pending ops are removed and agent continuations are restored.

Date: 2026-08-18
"""

from __future__ import annotations

from my_team.agent_runtime import ActionResult, AgentAction
from my_team.agent_tree import AgentTree
from my_team.models.activation import ReadyCandidate
from my_team.models.continuation import ContinuationPhase
from my_team.models.intent import (
    SubmitLLMRequest,
    SubmitToolRequest,
)
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
                "tools": ["read", "write", "ls", "delegate", "send_email"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


def _make_sim() -> Simulation:
    return Simulation(agent_tree=_make_tree())


def _ok_validated(intent) -> ActionResult:
    return ActionResult(
        action=AgentAction(
            action_type=intent.intent_type.value,
            tool_name=getattr(intent, "tool_name", ""),
            payload=dict(intent.payload),
        ),
        success=True,
        result_data={"validated": True},
    )


def _stage_failing_effect(sim: Simulation) -> None:
    """Stage a TASK_CREATE with a duplicate id to force rollback."""
    sim.task_tree.create(
        task_id="task.rollback_trigger",
        title="Existing",
        creator_agent_id="agent.root",
        owner_agent_id="agent.root",
    )
    sim._transaction_buffer.stage(
        EffectType.TASK_CREATE,
        "agent.root",
        "task.rollback_trigger",  # already exists → fails at commit
        data={
            "task_id": "task.rollback_trigger",
            "title": "Duplicate",
            "creator_agent_id": "agent.root",
            "owner_agent_id": "agent.root",
        },
    )


class TestRollbackRemovesPendingOps:
    """Rollback must undo pending ops registered this tick."""

    def test_llm_request_rollback_removes_op(self) -> None:
        """SubmitLLMRequest + triggering rollback → no new pending op."""
        sim = _make_sim()
        sim._config.max_concurrent_llm_requests = 4

        llm_intent = SubmitLLMRequest(
            agent_id="agent.root", messages=(),
            timeout_ticks=10, task_id="task.001",
        )
        plan = {"agent.root": [llm_intent]}
        validated = {"agent.root": [_ok_validated(llm_intent)]}
        sim._phase_act(
            0, plan,
            ready=[ReadyCandidate(agent_id="agent.root", events=(), tick=0)],
            validated=validated,
        )

        # Verify op was registered
        assert sim._pending_ops.count_in_flight("agent.root") == 1

        # Stage a failing effect to trigger rollback
        _stage_failing_effect(sim)
        sim._phase_commit(0, {})

        # After rollback: no in-flight ops from this tick
        assert sim._pending_ops.count_in_flight("agent.root") == 0
        # Continuation restored to FRESH
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.phase == ContinuationPhase.FRESH

    def test_tool_request_rollback_removes_op(self) -> None:
        """SubmitToolRequest (remote) + rollback → no new pending op."""
        sim = _make_sim()

        from tests.tool_helpers import register_remote_tool
        register_remote_tool(sim, "remote_calc")

        tool_intent = SubmitToolRequest(
            agent_id="agent.root",
            tool_name="remote_calc",
            arguments={"x": 1},
            timeout_ticks=10, task_id="task.002",
        )
        plan = {"agent.root": [tool_intent]}
        validated = {"agent.root": [_ok_validated(tool_intent)]}
        sim._phase_act(
            0, plan,
            ready=[ReadyCandidate(agent_id="agent.root", events=(), tick=0)],
            validated=validated,
        )

        assert sim._pending_ops.count_in_flight("agent.root") == 1

        _stage_failing_effect(sim)
        sim._phase_commit(0, {})

        assert sim._pending_ops.count_in_flight("agent.root") == 0
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.phase == ContinuationPhase.FRESH

    def test_request_id_reusable_after_rollback(self) -> None:
        """After rollback, the same request_id can be resubmitted."""
        sim = _make_sim()
        sim._config.max_concurrent_llm_requests = 4

        req_id = "req.reusable.001"
        llm_intent = SubmitLLMRequest(
            agent_id="agent.root", messages=(),
            timeout_ticks=10, task_id="task.003",
            request_id=req_id,
        )
        plan = {"agent.root": [llm_intent]}
        validated = {"agent.root": [_ok_validated(llm_intent)]}
        sim._phase_act(
            0, plan,
            ready=[ReadyCandidate(agent_id="agent.root", events=(), tick=0)],
            validated=validated,
        )

        _stage_failing_effect(sim)
        sim._phase_commit(0, {})

        # After rollback, same request_id should NOT be seen
        assert not sim._pending_ops.is_seen("agent.root", req_id)


class TestConsecutiveRollbacks:
    """Multiple consecutive rollbacks must not accumulate orphan ops."""

    def test_three_rollbacks_no_orphan_ops(self) -> None:
        """3 consecutive rollbacks → still 0 pending ops."""
        sim = _make_sim()
        sim._config.max_concurrent_llm_requests = 4

        # Pre-create the task once (used by _stage_failing_effect)
        sim.task_tree.create(
            task_id="task.rollback_trigger",
            title="Existing",
            creator_agent_id="agent.root",
            owner_agent_id="agent.root",
        )

        for tick in range(3):
            llm_intent = SubmitLLMRequest(
                agent_id="agent.root", messages=(),
                timeout_ticks=10, task_id=f"task轮回{tick}",
            )
            plan = {"agent.root": [llm_intent]}
            validated = {"agent.root": [_ok_validated(llm_intent)]}
            sim._phase_act(
                tick, plan,
                ready=[ReadyCandidate(
                    agent_id="agent.root", events=(), tick=tick,
                )],
                validated=validated,
            )

            # Stage a failing TASK_CREATE (duplicate) to trigger rollback
            sim._transaction_buffer.stage(
                EffectType.TASK_CREATE,
                "agent.root",
                "task.rollback_trigger",
                data={
                    "task_id": "task.rollback_trigger",
                    "title": "Duplicate",
                    "creator_agent_id": "agent.root",
                    "owner_agent_id": "agent.root",
                },
            )
            sim._phase_commit(tick, {})

        # After 3 rollbacks: no accumulated ops
        assert sim._pending_ops.count_in_flight("agent.root") == 0
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.continuation.phase == ContinuationPhase.FRESH
