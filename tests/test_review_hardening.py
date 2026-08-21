"""Review-round hardening tests (v0.7.0 review response).

Verifies:
- FILE_PATCH base_hash: commit-time content re-check — a same-tick
  write to the same file makes the patch stale → local patch_conflict,
  never a silent overwrite (no tick rollback)
- Effect group atomicity: delegate's TASK_CREATE + EMAIL_SEND commit or
  fail as one; a group member failing CommitValidate fails the whole
  group locally (no rollback)
- Structured validation error codes on ActionResult + audit details
- Scheduler claim requeue: after a tick ROLLBACK, claimed wake events
  are requeued so the agents re-activate and re-observe next tick
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from my_team.agent_runtime import ActionResult, AgentAction
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.models.activation import ReadyCandidate, WakeEventType
from my_team.models.intent import (
    CompleteTaskIntent,
    DelegateIntent,
    Intent,
    SubmitToolRequest,
)
from my_team.simulation import Simulation
from my_team.tool_manifest import OperationPolicy
from my_team.transaction import EffectStatus, EffectType


def _make_tree(tools: list[str]) -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": tools,
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "apply_patch", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _ok(intent: Intent) -> ActionResult:
    return ActionResult(
        action=AgentAction(
            action_type=intent.intent_type.value,
            tool_name=getattr(intent, "tool_name", ""),
            payload=dict(intent.payload),
        ),
        success=True,
        result_data={"validated": True},
    )


class TestPatchBaseHash:
    """Commit-time re-check: patch must not overwrite a same-tick write."""

    def _patch(self, path: str) -> str:
        return (
            "--- a/" + path + "\n+++ b/" + path + "\n"
            "@@ -1,1 +1,1 @@\n"
            "-hello\n"
            "+goodbye\n"
        )

    def test_stale_patch_rejected_at_act(self) -> None:
        """A write to the same path staged BEFORE the patch changes the
        base: with on-demand reads (committed state + own staged), the
        patch is validated against the staged content at Act and
        rejected as a conflict — never a silent overwrite, no tick
        rollback."""
        path = f"f-{uuid4().hex[:8]}.md"
        sim = Simulation(agent_tree=_make_tree(
            ["read", "write", "apply_patch"],
        ))
        target = sim._private_store.agent_home("agent.root") / path
        target.write_text("hello", encoding="utf-8")

        # Same tick: write first (staged), then the patch — which now
        # validates against the staged "interloper" content.
        sim._tool_registry.execute(
            ToolCtx(sim), "write", path=path, content="interloper",
        )
        patch_res = sim._tool_registry.execute(
            ToolCtx(sim), "apply_patch", path=path, patch=self._patch(path),
        )
        assert not patch_res.success
        assert patch_res.error_code == "patch_conflict"
        sim._phase_commit(0, {"agent.root": []})

        # The write applied; the stale patch never touched the file
        assert target.read_text(encoding="utf-8") == "interloper"
        assert sim.state_epoch == 0

    def test_clean_patch_commits(self) -> None:
        path = f"f-{uuid4().hex[:8]}.md"
        sim = Simulation(agent_tree=_make_tree(
            ["read", "write", "apply_patch"],
        ))
        target = sim._private_store.agent_home("agent.root") / path
        target.write_text("hello", encoding="utf-8")

        sim._tool_registry.execute(
            ToolCtx(sim), "apply_patch", path=path, patch=self._patch(path),
        )
        sim._phase_commit(0, {"agent.root": []})
        assert target.read_text(encoding="utf-8") == "goodbye"

    def test_patch_data_carries_hashes(self) -> None:
        path = f"f-{uuid4().hex[:8]}.md"
        sim = Simulation(agent_tree=_make_tree(
            ["read", "write", "apply_patch"],
        ))
        target = sim._private_store.agent_home("agent.root") / path
        target.write_text("hello", encoding="utf-8")
        result = sim._tool_registry.execute(
            ToolCtx(sim), "apply_patch", path=path, patch=self._patch(path),
        )
        assert result.success
        effect = [
            e for e in sim._transaction_buffer.get_effects("agent.root")
            if e.effect_type == EffectType.FILE_PATCH
        ][0]
        data = effect.data
        assert data["base_hash"] == result.data["base_hash"]
        assert data["patch_hash"] and data["new_content_hash"]
        assert len(data["base_hash"]) == 64  # sha256 hex


def ToolCtx(sim: Simulation):
    from my_team.agent_runtime import ToolContext
    return ToolContext(
        agent_id="agent.root", tick=0,
        allowed_tools=sim._tool_registry.get_allowed_tools("agent.root"),
    )


class TestEffectGroupAtomicity:
    """Delegate = TASK_CREATE + EMAIL_SEND: one group, all or nothing."""

    def test_delegate_stages_grouped_effects(self) -> None:
        sim = Simulation(agent_tree=_make_tree(["delegate"]))
        intent = DelegateIntent(
            agent_id="agent.root",
            recipient_agent_id="agent.research",
            task_title="T",
        )
        plan: dict[str, list[Intent]] = {"agent.root": [intent]}
        sim._phase_act(
            0, plan, ready=[], validated={"agent.root": [_ok(intent)]},
            snapshot=sim._build_snapshot(0),
        )
        effects = sim._transaction_buffer.get_effects("agent.root")
        types = {e.effect_type for e in effects}
        assert types == {EffectType.TASK_CREATE, EffectType.EMAIL_SEND}
        groups = {e.group_id for e in effects}
        assert len(groups) == 1 and groups != {""}
        assert all(e.atomicity == "group" for e in effects)

    def test_group_member_failure_fails_whole_group(self) -> None:
        """A TASK_UPDATE on a cancelled task in a group fails the whole
        group LOCALLY — no tick rollback, sibling email not sent."""
        sim = Simulation(agent_tree=_make_tree(["send_email"]))
        sim.task_tree.create(
            task_id="task.001", title="T1",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
        )
        sim.task_tree.cancel_task("task.001", tick=0)

        bad = CompleteTaskIntent(
            agent_id="agent.root", task_id="task.001", summary="x",
        )
        results = {"agent.root": [_ok(bad)]}
        sim._phase_act(
            0, {"agent.root": [bad]}, ready=[],
            validated=results, snapshot=sim._build_snapshot(0),
        )
        # Stage the email in the SAME atomic group as the task update
        # (the intent staged the TASK_UPDATE without a group — attach
        # both to an explicit group)
        for eff in sim._transaction_buffer.get_effects("agent.root"):
            eff.group_id = "g.delegate.001"
            eff.atomicity = "group"
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND, "agent.root", "email:agent.root",
            data={"from_agent": "agent.root", "to": ["x"], "subject": "s",
                  "body": "", "email_type": "progress", "task_id": ""},
            group_id="g.delegate.001", atomicity="group",
        )
        sim._phase_commit(0, results)

        statuses = {
            e.effect_type: e.status
            for e in sim._transaction_buffer.get_effects("agent.root")
        }
        assert statuses[EffectType.TASK_UPDATE] == EffectStatus.FAILED
        assert statuses[EffectType.EMAIL_SEND] == EffectStatus.FAILED
        assert sim._mail_system._all_emails == {}
        assert sim.state_epoch == 0  # local failure, no rollback


class TestStructuredErrorCodes:
    """Validate failures carry machine-readable error_code."""

    def _validate(self, sim: Simulation, intent: Intent) -> ActionResult:
        plan: dict[str, list[Intent]] = {"agent.root": [intent]}
        candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=0)
        return sim._phase_validate(0, plan, ready=[candidate])[
            "agent.root"
        ][0]

    def test_manifest_missing_code(self) -> None:
        sim = Simulation(agent_tree=_make_tree(["web_search"]))
        result = self._validate(sim, SubmitToolRequest(
            agent_id="agent.root", tool_name="web_search",
            arguments={"q": "x"},
        ))
        assert result.error_code == "TOOL_MANIFEST_MISSING"

    def test_policy_denied_code(self) -> None:
        sim = Simulation(agent_tree=_make_tree(["send_email"]))
        sim._tool_registry.set_policy(
            OperationPolicy(allowed=frozenset({"read"}))
        )
        result = self._validate(sim, SubmitToolRequest(
            agent_id="agent.root", tool_name="send_email",
            arguments={"to": ["x"], "subject": "s"},
        ))
        assert result.error_code == "POLICY_DENIED"

    def test_approval_required_code(self) -> None:
        sim = Simulation(agent_tree=_make_tree(["send_email"]))
        sim._tool_registry.set_policy(OperationPolicy(
            allowed=frozenset({"send_email"}),
            requires_approval=frozenset({"send_email"}),
        ))
        result = self._validate(sim, SubmitToolRequest(
            agent_id="agent.root", tool_name="send_email",
            arguments={"to": ["x"], "subject": "s"},
        ))
        assert result.error_code == "APPROVAL_REQUIRED"

    def test_task_and_deadline_codes(self) -> None:
        sim = Simulation(agent_tree=_make_tree([]))
        r1 = self._validate(sim, CompleteTaskIntent(
            agent_id="agent.root", task_id="task.nope", summary="s",
        ))
        assert r1.error_code == "TASK_NOT_FOUND"
        sim.task_tree.create(
            task_id="task.001", title="T",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
            deadline=sim.tick_engine.wall_now() - timedelta(minutes=1),
        )
        plan: dict[str, list[Intent]] = {"agent.root": [CompleteTaskIntent(
            agent_id="agent.root", task_id="task.001", summary="s",
        )]}
        candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=6)
        r2 = sim._phase_validate(6, plan, ready=[candidate])[
            "agent.root"
        ][0]
        assert r2.error_code == "DEADLINE_EXCEEDED"

    def test_budget_code(self) -> None:
        sim = Simulation(agent_tree=_make_tree([]))
        sim._config.max_concurrent_llm_requests = 0  # force denial
        from my_team.models.intent import SubmitLLMRequest
        result = self._validate(sim, SubmitLLMRequest(
            agent_id="agent.root", messages=(),
        ))
        assert result.error_code == "BUDGET_EXCEEDED"
        denied = sim.audit_log.for_event_type(AuditEventType.PERMISSION_DENIED)
        assert any(
            (e.details or {}).get("error_code") == "BUDGET_EXCEEDED"
            for e in denied
        )

    def test_capability_code(self) -> None:
        sim = Simulation(agent_tree=_make_tree([]))  # root has no send_email
        result = self._validate(sim, SubmitToolRequest(
            agent_id="agent.root", tool_name="send_email",
            arguments={"to": ["x"], "subject": "s"},
        ))
        assert result.error_code == "CAPABILITY_DENIED"


class TestSchedulerClaimRequeue:
    """After a tick rollback, claimed wake events are requeued."""

    def test_rollback_requeues_wake_events(self) -> None:
        from my_team.agent_runtime import AgentObservation, BaseAgent
        from my_team.models.activation import WakeCondition
        from my_team.models.continuation import AgentContinuation
        from my_team.models.intent import WritePrivateFileIntent

        # Unique path — private/ persists across test runs
        blocked = f"blocked-{uuid4().hex[:8]}"
        target_path = f"{blocked}/x.txt"

        class DirBlockedWriteAgent(BaseAgent):
            """Writes to a path that is a DIRECTORY → apply raises
            IsADirectoryError → full tick rollback."""

            def decide_intents(
                self,
                observation: AgentObservation,
                continuation: AgentContinuation | None = None,
            ) -> list[Intent]:
                return [WritePrivateFileIntent(
                    agent_id=self._agent_id,
                    path=target_path,
                    content="boom",
                )]

        sim = Simulation(agent_tree=_make_tree(
            ["read", "write", "delegate"],
        ))
        # Block the write: a directory occupies the target path
        home = sim._private_store.agent_home("agent.root")
        (home / blocked).mkdir(parents=True)
        (home / blocked / "x.txt").mkdir()

        agent = DirBlockedWriteAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent
        cond = sim.scheduler.get_wake_condition("agent.root")
        sim.scheduler.update_wake_condition(
            "agent.root",
            WakeCondition(
                event_types=cond.event_types | {WakeEventType.BOOTSTRAP},
                wake_at_tick=0,
            ),
        )

        # Tick 0: agent activated; its write is blocked → ROLLBACK
        sim.run_tick()
        hist = sim.scheduler.get_activation_history()
        assert len(hist) == 1
        assert hist[0].completed is False
        assert sim._last_tick_rolled_back is True

        # The claimed BOOTSTRAP event was requeued (not consumed/expired)
        requeued = [
            qe for qe in sim.scheduler.all_events()
            if qe.status.value == "queued"
            and qe.event.event_type == WakeEventType.BOOTSTRAP
        ]
        assert requeued, "rollback must requeue claimed wake events"

        # Tick 1: the requeued event re-activates the agent
        sim.run_tick()
        hist = sim.scheduler.get_activation_history()
        assert len(hist) == 2
        assert hist[1].tick == 1
        # Still blocked → rolls back again; the event re-queues forever
        assert hist[1].completed is False
        assert sim._last_tick_rolled_back is True
