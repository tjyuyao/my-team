"""Two-phase Validate hardening tests (v0.7.0 P1-2).

Verifies the split principle:
- PreValidate (Phase 6): "is this attempt allowed to try?" — manifest
  presence, operation policy (allowlist/approval), task validity
  (exists, deadline not passed)
- Act (Phase 7): commit-time budget re-check (registry + this-tick
  submissions — closes the window between PreValidate and submission)
- CommitValidate (Phase 8): "is it still committable now?" — TASK_UPDATE
  must target an existing, live, non-expired task; failures are LOCAL
  (effect failed) — never a full-tick rollback
"""

from __future__ import annotations

from my_team.agent_runtime import ActionResult, AgentAction
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.activation import ReadyCandidate
from my_team.models.intent import (
    CompleteTaskIntent,
    Intent,
    SubmitLLMRequest,
    SubmitToolRequest,
)
from my_team.models.task import TaskStatus
from my_team.simulation import Simulation
from my_team.tool_manifest import OperationPolicy
from my_team.transaction import EffectStatus


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write", "ls", "delegate", "send_email",
                          "web_search", "kb_write"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


def _ok_result(intent: Intent) -> ActionResult:
    return ActionResult(
        action=AgentAction(
            action_type=intent.intent_type.value,
            tool_name=getattr(intent, "tool_name", ""),
            payload=dict(intent.payload),
        ),
        success=True,
        result_data={"validated": True},
    )


def _validate(sim: Simulation, intent: Intent) -> ActionResult:
    plan: dict[str, list[Intent]] = {"agent.root": [intent]}
    candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=0)
    validated = sim._phase_validate(0, plan, ready=[candidate])
    return validated["agent.root"][0]


class TestPreValidateManifestPolicy:
    """Check 1d: manifest presence + operation policy."""

    def test_tool_without_manifest_denied(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        # web_search granted by tree but no manifest registered
        intent = SubmitToolRequest(
            agent_id="agent.root",
            tool_name="web_search",
            arguments={"query": "x"},
        )
        result = _validate(sim, intent)
        assert not result.success
        assert "no registered manifest" in (result.error or "")
        assert sim._pending_ops.pending_count == 0
        denied = sim.audit_log.for_event_type(AuditEventType.PERMISSION_DENIED)
        assert any("no_manifest" in (e.details or {}).get("reason", "")
                   for e in denied)

    def test_policy_denied_tool_rejected(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim._tool_registry.set_policy(
            OperationPolicy(allowed=frozenset({"read", "write", "ls"}))
        )
        intent = SubmitToolRequest(
            agent_id="agent.root",
            tool_name="send_email",
            arguments={"to": ["agent.root"], "subject": "s"},
        )
        result = _validate(sim, intent)
        assert not result.success
        assert "allowlist" in (result.error or "")
        denied = sim.audit_log.for_event_type(AuditEventType.PERMISSION_DENIED)
        assert any("policy_denied" in (e.details or {}).get("reason", "")
                   for e in denied)

    def test_requires_approval_rejected(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim._tool_registry.set_policy(OperationPolicy(
            allowed=frozenset({"send_email"}),
            requires_approval=frozenset({"send_email"}),
        ))
        intent = SubmitToolRequest(
            agent_id="agent.root",
            tool_name="send_email",
            arguments={"to": ["agent.root"], "subject": "s"},
        )
        result = _validate(sim, intent)
        assert not result.success
        assert "approval" in (result.error or "").lower()

    def test_policy_allowed_tool_passes(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim._tool_registry.set_policy(
            OperationPolicy(allowed=frozenset({"read", "write", "ls",
                                                "send_email"}))
        )
        intent = SubmitToolRequest(
            agent_id="agent.root",
            tool_name="send_email",
            arguments={"to": ["agent.root"], "subject": "s"},
        )
        result = _validate(sim, intent)
        assert result.success


class TestPreValidateTaskValidity:
    """Check 4: task existence + deadline."""

    def test_task_not_found_denied(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        intent = CompleteTaskIntent(
            agent_id="agent.root", task_id="task.nope",
            summary="done",
        )
        result = _validate(sim, intent)
        assert not result.success
        assert "not found" in (result.error or "")

    def test_task_deadline_passed_denied(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.task_tree.create(
            task_id="task.001", title="T1",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
            deadline_tick=5,
        )
        # Validate at tick 6 — deadline 5 has passed
        intent = CompleteTaskIntent(
            agent_id="agent.root", task_id="task.001",
            summary="done",
        )
        plan: dict[str, list[Intent]] = {"agent.root": [intent]}
        candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=6)
        validated = sim._phase_validate(6, plan, ready=[candidate])
        result = validated["agent.root"][0]
        assert not result.success
        assert "deadline" in (result.error or "")

    def test_task_within_deadline_passes(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.task_tree.create(
            task_id="task.001", title="T1",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
            deadline_tick=10,
        )
        intent = CompleteTaskIntent(
            agent_id="agent.root", task_id="task.001",
            summary="done",
        )
        assert _validate(sim, intent).success


class TestActBudgetRecheck:
    """配额仍够: commit-time re-check closes the PreValidate window."""

    def test_second_llm_request_rejected_at_act(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim._config.max_concurrent_llm_requests = 1
        intents = [
            SubmitLLMRequest(agent_id="agent.root", messages=()),
            SubmitLLMRequest(agent_id="agent.root", messages=()),
        ]
        plan: dict[str, list[Intent]] = {"agent.root": intents}
        validated = {"agent.root": [_ok_result(i) for i in intents]}

        results = sim._phase_act(
            0, plan, ready=[], validated=validated,
            snapshot=sim._build_snapshot(0),
        )
        outcomes = results["agent.root"]
        assert outcomes[0].success
        assert not outcomes[1].success
        assert "budget" in (outcomes[1].error or "")
        # Only ONE op registered — the second was not submitted
        assert sim._pending_ops.pending_count == 1


class TestCommitValidateTask:
    """CommitValidate: TASK_UPDATE targets a live task; failures local."""

    def _sim_with_task(
        self, task_id: str = "task.001",
    ) -> tuple[Simulation, str]:
        sim = Simulation(agent_tree=_make_tree())
        sim.task_tree.create(
            task_id=task_id, title="T1",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
        )
        return sim, task_id

    def test_update_cancelled_task_fails_locally(self) -> None:
        sim, task_id = self._sim_with_task()
        sim.task_tree.cancel_task(task_id, tick=0)

        intent = CompleteTaskIntent(
            agent_id="agent.root", task_id=task_id, summary="done",
        )
        result = _validate(sim, intent)
        assert result.success  # PreValidate doesn't check cancelled

        results = {"agent.root": [result]}
        sim._phase_act(0, {"agent.root": [intent]}, ready=[],
                       validated=results,
                       snapshot=sim._build_snapshot(0))
        sim._phase_commit(0, results)

        # Effect failed at CommitValidate — LOCAL failure
        effect = sim._transaction_buffer.get_effects_for_resource(task_id)[0]
        assert effect.status == EffectStatus.FAILED
        assert "cancelled" in (effect.error or "")
        # NO full-tick rollback: epoch untouched, task still cancelled
        assert sim.state_epoch == 0
        assert sim.task_tree.get(task_id).status == TaskStatus.CANCELLED

    def test_update_missing_task_fails_locally(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        intent = CompleteTaskIntent(
            agent_id="agent.root", task_id="task.ghost", summary="done",
        )
        # PreValidate rejects it — but stage directly to test Commit
        results = {"agent.root": [_ok_result(intent)]}
        sim._phase_act(0, {"agent.root": [intent]}, ready=[],
                       validated=results,
                       snapshot=sim._build_snapshot(0))
        sim._phase_commit(0, results)
        effect = sim._transaction_buffer.get_effects_for_resource(
            "task.ghost",
        )[0]
        assert effect.status == EffectStatus.FAILED
        assert "not found" in (effect.error or "")
        assert sim.state_epoch == 0

    def test_update_terminal_task_fails_locally(self) -> None:
        sim, task_id = self._sim_with_task()
        sim.task_tree.update_status(
            task_id, TaskStatus.COMPLETED, tick=0, allow_walk=True,
        )
        intent = CompleteTaskIntent(
            agent_id="agent.root", task_id=task_id, summary="done",
        )
        results = {"agent.root": [_ok_result(intent)]}
        sim._phase_act(0, {"agent.root": [intent]}, ready=[],
                       validated=results,
                       snapshot=sim._build_snapshot(0))
        sim._phase_commit(0, results)
        effect = sim._transaction_buffer.get_effects_for_resource(task_id)[0]
        assert effect.status == EffectStatus.FAILED
        assert "terminal" in (effect.error or "")

    def test_valid_update_commits_alongside_failed_one(self) -> None:
        """A failed task update does not drag down other effects."""
        sim = Simulation(agent_tree=_make_tree())
        sim.task_tree.create(
            task_id="task.bad", title="Bad",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
        )
        sim.task_tree.create(
            task_id="task.good", title="Good",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
        )
        sim.task_tree.cancel_task("task.bad", tick=0)

        bad = CompleteTaskIntent(
            agent_id="agent.root", task_id="task.bad", summary="x",
        )
        good = CompleteTaskIntent(
            agent_id="agent.root", task_id="task.good", summary="y",
        )
        results = {"agent.root": [_ok_result(bad), _ok_result(good)]}
        sim._phase_act(0, {"agent.root": [bad, good]}, ready=[],
                       validated=results,
                       snapshot=sim._build_snapshot(0))
        sim._phase_commit(0, results)

        bad_eff = sim._transaction_buffer.get_effects_for_resource(
            "task.bad",
        )[0]
        good_eff = sim._transaction_buffer.get_effects_for_resource(
            "task.good",
        )[0]
        assert bad_eff.status == EffectStatus.FAILED
        assert good_eff.status == EffectStatus.COMMITTED
        assert sim.task_tree.get("task.good").status == TaskStatus.COMPLETED
        assert sim.state_epoch == 0  # no rollback

    def test_update_past_deadline_fails_at_commit(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        sim.task_tree.create(
            task_id="task.001", title="T1",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
            deadline_tick=5,
        )
        intent = CompleteTaskIntent(
            agent_id="agent.root", task_id="task.001", summary="done",
        )
        results = {"agent.root": [_ok_result(intent)]}
        sim._phase_act(0, {"agent.root": [intent]}, ready=[],
                       validated=results,
                       snapshot=sim._build_snapshot(0))
        # Commit at tick 6 — deadline 5 passed between Act and Commit
        sim._phase_commit(6, results)
        effect = sim._transaction_buffer.get_effects_for_resource(
            "task.001",
        )[0]
        assert effect.status == EffectStatus.FAILED
        assert "deadline" in (effect.error or "")


class TestCommitValidatePrinciple:
    """The PreValidate/CommitValidate split is documented in code."""

    def test_validate_docstring_states_principle(self) -> None:
        import inspect

        from my_team.simulation import Simulation as S

        doc = inspect.getdoc(S._phase_validate) or ""
        assert "allowed to try" in doc.lower()
        doc8 = inspect.getdoc(S._phase_commit) or ""
        assert "committable" in doc8
