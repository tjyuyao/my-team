"""Tests for v0.10-12a: Human Worker (kind=human Agent, SPEC §10.1).

Covers:
- kind=human agents get a dedicated HumanWorkerRuntime (UI-queue driven,
  never emits LLM/tool intents)
- Manager delegates to a human worker like to any AI worker
- Human UI actions accept/complete/fail ingress as IngressEvent
  (source="human"), route to the assignee, and are translated to
  Intents through the SAME transaction path (no separate channel)
- Rejected when: task is terminal, assignee is not kind=human,
  action is unknown
- Structured escalation: an expired HUMAN task escalates to the
  assigner (on/mode/target, one upgrade — not a hardcoded ladder)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from my_team.agent_tree import AgentTree
from my_team.models.activation import ReadyCandidate
from my_team.models.intent import DelegateIntent
from my_team.models.task import TaskStatus

_BASE = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)


def _tree():
    """root (llm) → human worker (kind=human)."""
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.human1"],
                "tools": ["read", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": False},
            },
            {
                "agent_id": "agent.human1",
                "display_name": "Human 1",
                "role": "worker",
                "kind": "human",
                "parent_id": "agent.root",
                "children": [],
                "tools": [],
                "can_delegate": False,
                "metadata": {},
            },
        ],
    })


def _make_sim():
    from my_team.simulation import Simulation

    sim = Simulation(agent_tree=_tree())
    engine = sim.tick_engine
    engine._config.tick_duration_value = 10
    engine._config.tick_duration_unit = "minutes"
    engine._anchor = _BASE
    return sim


def _delegate(sim, title="Human task", deadline=None):
    """Real DelegateIntent through Validate → Act → Commit."""
    intent = DelegateIntent(
        agent_id="agent.root",
        recipient_agent_id="agent.human1",
        task_title=title,
        task_description="please handle",
        deadline=deadline or (_BASE + timedelta(hours=2)),
    )
    tick = sim.tick_engine.current_tick
    plan: dict = {"agent.root": [intent]}
    candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=tick)
    validated = sim._phase_validate(tick, plan, ready=[candidate])
    act_results = sim._phase_act(
        tick, plan, ready=[candidate], validated=validated,
    )
    sim._phase_commit(tick, act_results)
    return act_results


def _human_task(sim) -> str:
    """ID of the delegated human task."""
    for tid in sim.task_tree.all_ids():
        t = sim.task_tree.get(tid)
        if t.assignee_agent_id == "agent.human1":
            return tid
    raise AssertionError("no human task")


def _run_tick(sim):
    return sim.run_tick()


class TestHumanWorkerRuntime:
    def test_human_agent_uses_human_worker_runtime(self):
        sim = _make_sim()
        runtime = sim._runtimes["agent.human1"]
        assert type(runtime).__name__ == "HumanWorkerRuntime"
        assert runtime._tool_context.allowed_tools == frozenset()

    def test_human_worker_delegation_like_ai_worker(self):
        sim = _make_sim()
        _delegate(sim)
        task = sim.task_tree.get(_human_task(sim))
        assert task.status == TaskStatus.ASSIGNED
        assert task.assigner_agent_id == "agent.root"
        assert task.assignee_agent_id == "agent.human1"


class TestHumanActions:
    def test_accept_ingresses_and_translates(self):
        sim = _make_sim()
        _delegate(sim)
        tid = _human_task(sim)

        result = sim.human_control.accept_task(tid)
        assert result.success
        assert sim._ingress.pending_count() == 1

        _run_tick(sim)
        assert sim.task_tree.get(tid).status == TaskStatus.ACCEPTED

    def test_complete_ingresses_and_translates(self):
        sim = _make_sim()
        _delegate(sim)
        tid = _human_task(sim)

        sim.human_control.accept_task(tid)
        _run_tick(sim)
        assert sim.task_tree.get(tid).status == TaskStatus.ACCEPTED

        sim.human_control.complete_task(tid, summary="done by human")
        _run_tick(sim)
        task = sim.task_tree.get(tid)
        assert task.status == TaskStatus.COMPLETED
        assert task.metadata.get("summary") == "done by human"

    def test_fail_ingresses_and_translates(self):
        sim = _make_sim()
        _delegate(sim)
        tid = _human_task(sim)

        sim.human_control.fail_task(tid, reason="cannot do")
        _run_tick(sim)
        task = sim.task_tree.get(tid)
        assert task.status == TaskStatus.FAILED
        assert task.metadata.get("reason") == "cannot do"

    def test_action_on_terminal_task_rejected(self):
        sim = _make_sim()
        _delegate(sim)
        tid = _human_task(sim)

        sim.human_control.accept_task(tid)
        _run_tick(sim)
        sim.human_control.complete_task(tid)
        _run_tick(sim)
        assert sim.task_tree.get(tid).status == TaskStatus.COMPLETED

        result = sim.human_control.complete_task(tid)
        assert not result.success
        # Terminal task cannot be resurrected: no ingress staged.
        assert sim._ingress.pending_count() == 0

    def test_action_on_non_human_assignee_rejected(self):
        sim = _make_sim()
        # task assigned to root (kind=llm) — not a human worker
        sim.task_tree.create(
            task_id="t.llm", title="llm task",
            assigner_agent_id="agent.root", assignee_agent_id="agent.root",
            status=TaskStatus.ASSIGNED,
        )
        result = sim.human_control.complete_task("t.llm")
        assert not result.success
        assert "not a kind=human worker" in result.message

    def test_unknown_action_rejected(self):
        sim = _make_sim()
        _delegate(sim)
        tid = _human_task(sim)
        result = sim.human_control.submit_task_action(tid, "explode")
        assert not result.success
        assert "Unknown human action" in result.message


class TestStructuredEscalation:
    def test_expired_human_task_escalates_to_assigner(self):
        sim = _make_sim()
        # Task with a deadline already in the past at creation time.
        _delegate(sim, deadline=_BASE - timedelta(minutes=5))
        tid = _human_task(sim)

        _run_tick(sim)
        task = sim.task_tree.get(tid)
        # TimeoutChecker expires it (Publish, post-commit)
        assert task.status == TaskStatus.EXPIRED

        # Escalation email to the assigner.
        outbox_entries = [
            e for e in sim._outbox._entries.values()
            if e.task_id == tid and "ESCALATION" in e.subject
        ]
        assert len(outbox_entries) == 1
        entry = outbox_entries[0]
        assert entry.to == ["agent.root"]
        assert "on=unresolved" in entry.body
        assert "mode=advise" in entry.body
        assert "target=agent.root" in entry.body

        # Escalation audit recorded (structured on/mode/target).
        escalations = [
            e for e in sim._audit_log._entries
            if getattr(e, "details", {}).get("failure_type") == "timeout"
            and getattr(e, "details", {}).get("escalation")
        ]
        assert len(escalations) == 1
        assert escalations[0].details["escalation"] == {
            "on": "unresolved", "mode": "advise", "target": "agent.root",
        }

    def test_ai_task_expiry_does_not_escalate_to_human_path(self):
        sim = _make_sim()
        # A non-human task (assignee root, kind=llm) — expiry must NOT
        # produce a human-style escalation email.
        sim.task_tree.create(
            task_id="t.llm", title="llm task",
            assigner_agent_id="agent.root", assignee_agent_id="agent.root",
            status=TaskStatus.IN_PROGRESS,
            deadline=_BASE - timedelta(minutes=5),
        )
        _run_tick(sim)
        esc = [
            e for e in sim._outbox._entries.values()
            if "ESCALATION" in e.subject
        ]
        assert esc == []
