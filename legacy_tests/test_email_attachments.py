"""T8b: Email attachments (邮件系统侧).

Verifies:
- Email.attachments: list[AttachmentRef] carried on the message.
- send_email supports an attachments arg; refs flow through the outbox
  and Materialise onto the delivered Email.
- The recipient's context (snapshot emails) shows the attachment 清单 —
  refs, not payloads.
- The recipient reads an authorized SharedKB attachment via kb_read; an
  unauthorized attachment is refused (permission check).
- Email-attachment model + serialization round-trip.
"""
from __future__ import annotations

from my_team.agent_runtime import (
    ActionPlan,
    AgentAction,
    BaseAgent,
    ToolContext,
    action_plan_to_intents,
)
from my_team.agent_tree import AgentTree
from my_team.asset_store import AttachmentRef
from my_team.outbox import Outbox, OutboxEntry
from my_team.shared_kb import PermissionRule
from my_team.simulation import Simulation
from my_team.transaction import EffectType


def _tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.a",
                "display_name": "A",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.b"],
                "tools": ["read", "write", "send_email", "kb_read"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.b",
                "display_name": "B",
                "role": "worker",
                "parent_id": "agent.a",
                "children": [],
                "tools": ["read", "write", "send_email", "kb_read"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _sim() -> Simulation:
    sim = Simulation(agent_tree=_tree())
    sim._permission_engine.add_rules([
        PermissionRule(
            scope="project/*", principal="agent.a",
            allow=["read", "list", "create", "write", "kb_write"],
        ),
        PermissionRule(
            scope="project/reports/*", principal="agent.b",
            allow=["read", "list"],
        ),
    ])
    # A shared report that A attaches to the email
    sim._shared_kb.create(
        path="project/reports/q1.md", agent_id="agent.a",
        content="Q1 report draft", tick=0,
    )
    return sim


def _ctx(sim: Simulation, agent_id: str) -> ToolContext:
    return ToolContext(
        agent_id=agent_id, tick=0,
        allowed_tools=sim._tool_registry.get_allowed_tools(agent_id),
    )


def make_ref(path: str, version: int = 1) -> dict:
    return {
        "ref_type": "shared_kb",
        "path": path,
        "version": version,
        "hash": "def123",
        "size": 18,
        "mime": "text/markdown",
    }


class TestAttachmentRefModel:
    def test_attachments_on_email(self) -> None:
        sim = _sim()
        sim._transaction_buffer.stage(
            EffectType.EMAIL_SEND, "agent.a", "email:agent.a",
            data={
                "from_agent": "agent.a",
                "to": ["agent.b"],
                "subject": "Report",
                "body": "see attached",
                "attachments": [
                    AttachmentRef(
                        ref_type="shared_kb",
                        path="project/reports/q1.md", version=1,
                        hash="h", size=18, mime="text/markdown",
                    ),
                ],
            },
        )
        sim._phase_commit(0, {})
        email = list(sim._mail_system._all_emails.values())[0]
        assert len(email.attachments) == 1
        assert email.attachments[0].path == "project/reports/q1.md"
        assert email.attachments[0].ref_type == "shared_kb"


class TestSendEmailTool:
    def test_send_email_with_attachments(self) -> None:
        sim = _sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.a"), "send_email",
            to=["agent.b"], subject="Report", body="see attached",
            attachments=[make_ref("project/reports/q1.md")],
        )
        assert result.success
        sim._phase_commit(0, {})
        email = list(sim._mail_system._all_emails.values())[0]
        assert email.to == ["agent.b"]
        assert email.attachments[0].version == 1

    def test_outbox_entry_carries_attachments(self) -> None:
        sim = _sim()
        sim._tool_registry.execute(
            _ctx(sim, "agent.a"), "send_email",
            to=["agent.b"], subject="R", body="b",
            attachments=[make_ref("project/reports/q1.md")],
        )
        sim._phase_commit(0, {})
        # Dispatch happens synchronously in _phase_commit → all DISPATCHED
        delivered = sim._outbox.entries_by_status(
            __import__("my_team.outbox", fromlist=["OutboxStatus"]).OutboxStatus.DISPATCHED,
        )
        assert len(delivered) == 1
        assert delivered[0].attachments[0].ref_type == "shared_kb"


class TestRecipientContext:
    def test_attachment_manifest_visible_in_snapshot(self) -> None:
        """After delivery, the recipient's context (snapshot) shows the
        attachment 清单 — refs, not the payload."""
        sim = _sim()
        sim._tool_registry.execute(
            _ctx(sim, "agent.a"), "send_email",
            to=["agent.b"], subject="Report", body="see attached",
            attachments=[make_ref("project/reports/q1.md", version=1)],
        )
        sim._phase_commit(0, {})
        # Deliver at the latency tick (email_delivery_latency_ticks=1)
        sim._mail_system.deliver(1)

        snapshot = sim._build_snapshot(1)
        b_emails = [e for e in snapshot["emails"] if "agent.b" in e["to"]]
        assert len(b_emails) == 1
        assert b_emails[0]["attachments"][0]["path"] == "project/reports/q1.md"
        assert "body" in b_emails[0]  # manifest alongside body
        # Refs, not payload — no content field on the attachment
        assert "content" not in b_emails[0]["attachments"][0]


class TestAttachmentReadAuthorized:
    def test_recipient_reads_authorized_kb_attachment(self) -> None:
        """agent.b may read project/reports/* — kb_read succeeds."""
        sim = _sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.b"), "kb_read",
            path="project/reports/q1.md",
        )
        assert result.success
        assert result.data["content"] == "Q1 report draft"

    def test_recipient_refused_unauthorized_attachment(self) -> None:
        """agent.a's private report project/roadmap.md — agent.b lacks
        read permission, so the read is refused."""
        sim = _sim()
        sim._shared_kb.create(
            path="project/roadmap.md", agent_id="agent.a",
            content="confidential roadmap", tick=0,
        )
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.b"), "kb_read",
            path="project/roadmap.md",
        )
        assert not result.success
        assert result.error_code == "permission_denied"


class TestOutboxPersistence:
    def test_outbox_entry_attachment_roundtrip(self) -> None:
        ob = Outbox()
        entry = ob.stage(
            from_agent="a", to=["b"], subject="S", body="b",
            attachments=[AttachmentRef(
                ref_type="shared_kb", path="p.md", version=2,
                hash="h", size=5, mime="text/markdown",
            )],
        )
        dump = entry.model_dump(mode="json")
        revived = OutboxEntry.model_validate(dump)
        assert revived.attachments[0].version == 2
        assert revived.attachments[0].path == "p.md"


class TestEmailIntentPipeline:
    def test_send_email_intent_carries_attachments(self) -> None:
        """A real tick: the agent sends an email with an attachment via
        SendEmailIntent; the delivered email carries the ref."""
        sim = _sim()

        class MailAgent(BaseAgent):
            def decide_intents(self, observation, continuation=None):
                return action_plan_to_intents(ActionPlan(
                    agent_id="agent.a", tick=observation.tick,
                    actions=[AgentAction(
                        action_type="send_email",
                        tool_name="send_email",
                        payload={
                            "to": ["agent.b"],
                            "subject": "Report",
                            "body": "see attached",
                            "attachments": [
                                make_ref("project/reports/q1.md", version=1),
                            ],
                        },
                    )],
                ))

        agent = MailAgent("agent.a")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.a"] = agent
        sim.run_tick()
        sim._mail_system.deliver(1)

        email = list(sim._mail_system._all_emails.values())[0]
        assert email.to == ["agent.b"]
        assert email.attachments[0].path == "project/reports/q1.md"
