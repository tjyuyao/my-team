"""Tests for the email outbox with idempotent delivery.

Verifies:
- STAGED → COMMITTED → DISPATCHING → DISPATCHED lifecycle
- Idempotency: duplicate staging with same key returns existing entry
- Retry: failed dispatches retried up to max_retries, then DEAD
- Integration: EMAIL_SEND effects flow through the outbox
"""

from __future__ import annotations

from my_team.agent_runtime import (
    ActionPlan,
    AgentAction,
    BaseAgent,
    action_plan_to_intents,
)
from my_team.agent_tree import AgentTree
from my_team.outbox import Outbox, OutboxStatus
from my_team.simulation import Simulation


class TestOutboxLifecycle:
    """Outbox entry lifecycle."""

    def test_stage_commit_dispatch(self) -> None:
        """STAGED → COMMITTED → DISPATCHED via deliver callback."""
        outbox = Outbox()
        entry = outbox.stage(
            from_agent="agent.root",
            to=["agent.research"],
            subject="Hello",
            body="World",
        )
        assert entry.status == OutboxStatus.STAGED

        outbox.commit(entry.entry_id)
        assert entry.status == OutboxStatus.COMMITTED

        delivered: list[str] = []

        def deliver(e) -> None:
            delivered.append(e.subject)

        dispatched, failed = outbox.dispatch(deliver, current_tick=1)
        assert len(dispatched) == 1
        assert len(failed) == 0
        assert delivered == ["Hello"]
        assert entry.status == OutboxStatus.DISPATCHED
        assert entry.dispatched_at_tick == 1

    def test_idempotency_duplicate_stage(self) -> None:
        """Staging with the same idempotency_key returns the existing entry."""
        outbox = Outbox()
        e1 = outbox.stage(
            from_agent="agent.root",
            to=["agent.research"],
            subject="Same",
            idempotency_key="stable-key-1",
        )
        e2 = outbox.stage(
            from_agent="agent.root",
            to=["agent.research"],
            subject="Same",
            idempotency_key="stable-key-1",
        )
        assert e1.entry_id == e2.entry_id
        assert outbox.pending_count == 1

    def test_failed_dispatch_retries(self) -> None:
        """Failed dispatch is retried up to max_retries, then DEAD."""
        outbox = Outbox(max_retries=2)
        entry = outbox.stage(
            from_agent="agent.root",
            to=["agent.research"],
            subject="Flaky",
        )
        outbox.commit(entry.entry_id)

        def fail_deliver(e) -> None:
            raise RuntimeError("mail server down")

        # Attempt 1: fails, retry scheduled
        _, failed = outbox.dispatch(fail_deliver, current_tick=1)
        assert len(failed) == 1
        assert entry.status == OutboxStatus.COMMITTED  # retryable
        assert entry.attempt_count == 1
        assert entry.next_retry_tick == 2

        # Attempt 2 at tick 2: fails again
        _, failed = outbox.dispatch(fail_deliver, current_tick=2)
        assert entry.attempt_count == 2

        # Attempt 3 at tick 3: exceeds max_retries → DEAD
        _, failed = outbox.dispatch(fail_deliver, current_tick=3)
        assert entry.status == OutboxStatus.DEAD
        assert outbox.dead_count == 1

    def test_retry_not_before_next_retry_tick(self) -> None:
        """Entry is not retried before next_retry_tick."""
        outbox = Outbox(max_retries=3)
        entry = outbox.stage(
            from_agent="agent.root", to=["agent.research"], subject="T",
        )
        outbox.commit(entry.entry_id)

        def fail_deliver(e) -> None:
            raise RuntimeError("down")

        _, _ = outbox.dispatch(fail_deliver, current_tick=1)
        assert entry.next_retry_tick == 2

        # Tick 2 skipped? No — next_retry_tick=2 means eligible AT tick 2.
        # Try at tick 1 again: not eligible (next_retry=2 > 1)
        # But dispatch only processes COMMITTED; it would attempt again.
        # Actually the guard is: if next_retry_tick and current < next → skip
        outbox._entries[entry.entry_id].status = OutboxStatus.COMMITTED
        _, failed = outbox.dispatch(fail_deliver, current_tick=1)
        assert len(failed) == 0  # skipped, not retried before tick 2

    def test_rollback_removes_staged(self) -> None:
        """rollback() removes a staged (uncommitted) entry."""
        outbox = Outbox()
        entry = outbox.stage(
            from_agent="agent.root", to=["agent.research"], subject="Temp",
        )
        outbox.rollback(entry.entry_id)
        assert outbox.get(entry.entry_id) is None
        assert outbox.pending_count == 0

    def test_summary(self) -> None:
        outbox = Outbox()
        outbox.stage(from_agent="a", to=["b"], subject="1")
        outbox.stage(from_agent="a", to=["b"], subject="2")
        summary = outbox.summary()
        assert summary["total"] == 2
        assert summary["by_status"]["staged"] == 2


class TestOutboxSimulationIntegration:
    """EMAIL_SEND effects flow through the outbox in the simulation."""

    def test_email_effect_goes_through_outbox(self) -> None:
        """send_email intent → outbox entry → dispatched to MailSystem."""
        sim = Simulation(agent_tree=_make_tree_single())
        agent = EmailAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()

        # Email reached the MailSystem
        assert len(sim._mail_system._all_emails) == 1
        email = list(sim._mail_system._all_emails.values())[0]
        assert email.subject == "Outbox test"

        # Outbox entry dispatched
        dispatched = sim._outbox.entries_by_status(OutboxStatus.DISPATCHED)
        assert len(dispatched) == 1
        assert dispatched[0].subject == "Outbox test"
        assert sim._outbox.pending_count == 0


def _make_tree_single() -> AgentTree:
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


class EmailAgent(BaseAgent):
    """Agent that sends an email via the intent pipeline."""

    def decide_intents(self, observation, continuation=None):
        plan = ActionPlan(
            agent_id="agent.root",
            tick=observation.tick,
            actions=[AgentAction(
                action_type="send_email",
                tool_name="send_email",
                payload={
                    "to": ["agent.research"],
                    "subject": "Outbox test",
                    "body": "through the outbox",
                },
            )],
        )
        return action_plan_to_intents(plan)
