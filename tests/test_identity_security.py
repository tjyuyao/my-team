"""Tests for identity enforcement and permission binding.

Covers review gaps §8.3 (identity), §8.13 (Pydantic model mutability).
"""

import pytest

from my_team.agent_runtime import (
    MANAGER_TOOLS,
    ROOT_TOOLS,
    WORKER_TOOLS,
    ToolContext,
    ToolPermissionError,
    ToolRegistry,
)
from my_team.file_ops import FileOps
from my_team.identity import (
    ConfigModificationError,
    IdentityEnforcer,
    IdentityError,
    SpoofedSenderError,
)
from my_team.mailbox import EmailType, MailSystem
from my_team.private_store import PrivateStore, PrivateStoreConfig
from my_team.shared_kb import (
    PermissionEngine,
    PermissionRule,
    SharedKB,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_agent("agent.root", ROOT_TOOLS)
    reg.register_agent("agent.research", MANAGER_TOOLS)
    reg.register_agent("agent.web_research", WORKER_TOOLS)
    return reg


@pytest.fixture
def enforcer(tool_registry) -> IdentityEnforcer:
    return IdentityEnforcer(tool_registry)


# ---------------------------------------------------------------------------
# ToolContext immutability
# ---------------------------------------------------------------------------

class TestToolContextImmutability:
    def test_tool_context_frozen(self):
        ctx = ToolContext(agent_id="agent.a", tick=5)
        with pytest.raises(AttributeError):
            ctx.agent_id = "agent.b"  # type: ignore[misc]

    def test_tool_context_cannot_add_fields(self):
        ctx = ToolContext(agent_id="agent.a")
        with pytest.raises(AttributeError):
            ctx.new_field = "value"  # type: ignore[attr-defined]

    def test_tool_context_equality(self):
        ctx1 = ToolContext(agent_id="agent.a", tick=5)
        ctx2 = ToolContext(agent_id="agent.a", tick=5)
        assert ctx1 == ctx2

    def test_tool_context_inequality(self):
        ctx1 = ToolContext(agent_id="agent.a", tick=5)
        ctx2 = ToolContext(agent_id="agent.b", tick=5)
        assert ctx1 != ctx2


# ---------------------------------------------------------------------------
# IdentityEnforcer — ToolContext creation
# ---------------------------------------------------------------------------

class TestIdentityEnforcer:
    def test_create_tool_context(self, enforcer):
        ctx = enforcer.create_tool_context("agent.root", tick=5)
        assert ctx.agent_id == "agent.root"
        assert ctx.tick == 5
        assert ctx.allowed_tools == ROOT_TOOLS

    def test_create_context_unknown_agent(self, enforcer):
        with pytest.raises(IdentityError, match="not registered"):
            enforcer.create_tool_context("agent.unknown")

    def test_context_agent_id_from_registry(self, enforcer):
        """ToolContext.agent_id comes from registry, not from caller."""
        ctx = enforcer.create_tool_context("agent.research")
        assert ctx.agent_id == "agent.research"
        assert ctx.allowed_tools == MANAGER_TOOLS


# ---------------------------------------------------------------------------
# Email sender validation
# ---------------------------------------------------------------------------

class TestEmailSenderValidation:
    def test_validate_sender_matches(self, enforcer):
        ctx = enforcer.create_email_context("agent.root")
        verified = enforcer.validate_sender(ctx, "agent.root")
        assert verified == "agent.root"

    def test_validate_sender_spoofed(self, enforcer):
        ctx = enforcer.create_email_context("agent.research")
        with pytest.raises(SpoofedSenderError) as exc_info:
            enforcer.validate_sender(ctx, "agent.root")
        assert exc_info.value.claimed == "agent.root"
        assert exc_info.value.actual == "agent.research"

    def test_system_sets_from_agent(self, enforcer):
        """System sets from_agent from context, ignoring caller's claim."""
        ctx = enforcer.create_email_context("agent.web_research")
        email_kwargs = enforcer.wrap_email_creation(
            context=ctx,
            to=["agent.research"],
            subject="Report",
            body="Data collected",
            email_type=EmailType.RESULT,
            tick=5,
        )
        # from_agent should be from context, not caller
        assert email_kwargs["from_agent"] == "agent.web_research"

    def test_mailbox_uses_system_sender(self, enforcer):
        """MailSystem.create_email should use verified sender."""
        mail = MailSystem()
        mail.register_agent("agent.root")
        mail.register_agent("agent.research")

        ctx = enforcer.create_email_context("agent.research")
        email_kwargs = enforcer.wrap_email_creation(
            context=ctx,
            to=["agent.root"],
            subject="Test",
            body="Hello",
            email_type=EmailType.PROGRESS,
            tick=0,
        )

        email = mail.create_email(**email_kwargs)
        assert email.from_agent == "agent.research"


# ---------------------------------------------------------------------------
# Tool access validation
# ---------------------------------------------------------------------------

class TestToolAccessValidation:
    def test_validate_tool_access_allowed(self, enforcer):
        ctx = enforcer.create_tool_context("agent.root")
        enforcer.validate_tool_access(ctx, "read")  # should not raise

    def test_validate_tool_access_denied(self, enforcer):
        ctx = enforcer.create_tool_context("agent.root")
        with pytest.raises(ToolPermissionError):
            enforcer.validate_tool_access(ctx, "send_email")

    def test_root_agent_cannot_send_email(self, enforcer):
        ctx = enforcer.create_tool_context("agent.root")
        assert "send_email" not in ctx.allowed_tools

    def test_research_agent_can_delegate(self, enforcer):
        ctx = enforcer.create_tool_context("agent.research")
        assert "delegate" in ctx.allowed_tools

    def test_web_research_cannot_delegate(self, enforcer):
        ctx = enforcer.create_tool_context("agent.web_research")
        assert "delegate" not in ctx.allowed_tools


# ---------------------------------------------------------------------------
# File access enforcement
# ---------------------------------------------------------------------------

class TestFileAccessEnforcement:
    def test_wrap_file_read_uses_context(self, enforcer):
        """File read uses context.agent_id, not caller's agent_id."""
        ctx = enforcer.create_tool_context("agent.research")
        store = PrivateStore(PrivateStoreConfig(base_path="/tmp/test_id"))
        store.initialize_agent("agent.research")
        file_ops = FileOps(private_store=store)

        result = enforcer.wrap_file_read(ctx, file_ops, "workspace/test.txt")
        # Should fail because file doesn't exist, but the agent_id is correct
        assert not result.success
        assert result.agent_id == "agent.research"

    def test_wrap_file_write_uses_context(self, enforcer):
        """File write uses context.agent_id."""
        ctx = enforcer.create_tool_context("agent.research")
        store = PrivateStore(PrivateStoreConfig(base_path="/tmp/test_id2"))
        store.initialize_agent("agent.research")
        file_ops = FileOps(private_store=store)

        result = enforcer.wrap_file_write(
            ctx, file_ops, "workspace/test.txt", "content"
        )
        assert result.success
        assert result.agent_id == "agent.research"


# ---------------------------------------------------------------------------
# Shared KB access enforcement
# ---------------------------------------------------------------------------

class TestSharedKBAccessEnforcement:
    def test_validate_shared_kb_access_allowed(self, enforcer):
        ctx = enforcer.create_tool_context("agent.research")
        permissions = PermissionEngine([
            PermissionRule(
                scope="project/research/*",
                principal="agent.research",
                allow=["read", "write", "create"],
            ),
        ])
        enforcer.validate_shared_kb_access(
            ctx, "project/research/report.md", "read", permissions
        )

    def test_validate_shared_kb_access_denied(self, enforcer):
        ctx = enforcer.create_tool_context("agent.web_research")
        permissions = PermissionEngine([
            PermissionRule(
                scope="project/research/*",
                principal="agent.research",
                allow=["read", "write"],
            ),
        ])
        with pytest.raises(IdentityError, match="denied"):
            enforcer.validate_shared_kb_access(
                ctx, "project/research/report.md", "write", permissions
            )

    def test_wrap_shared_kb_read(self, enforcer):
        ctx = enforcer.create_tool_context("agent.research")
        permissions = PermissionEngine([
            PermissionRule(
                scope="project/*",
                principal="agent.research",
                allow=["read", "create"],
            ),
        ])
        kb = SharedKB(permissions=permissions)
        kb.create(
            "project/report.md",
            agent_id="agent.research",
            content="test",
        )

        resource = enforcer.wrap_shared_kb_read(ctx, kb, "project/report.md")
        assert resource.content == "test"


# ---------------------------------------------------------------------------
# Config modification prevention
# ---------------------------------------------------------------------------

class TestConfigModificationPrevention:
    def test_prevent_tools_modification(self, enforcer):
        with pytest.raises(ConfigModificationError):
            enforcer.prevent_config_modification("agent.root", "tools")

    def test_prevent_permissions_modification(self, enforcer):
        with pytest.raises(ConfigModificationError):
            enforcer.prevent_config_modification("agent.root", "shared_kb_permissions")

    def test_prevent_parent_modification(self, enforcer):
        with pytest.raises(ConfigModificationError):
            enforcer.prevent_config_modification("agent.research", "parent_id")


# ---------------------------------------------------------------------------
# Delegation validation
# ---------------------------------------------------------------------------

class TestDelegationValidation:
    def test_validate_delegation_allowed(self, enforcer):
        from my_team.agent_tree import AgentTree

        tree = AgentTree.from_dict({
            "agents": [
                {
                    "agent_id": "agent.root",
                    "display_name": "Root",
                    "role": "root",
                    "parent_id": None,
                    "children": ["agent.research"],
                    "tools": ["read", "write", "ls", "delegate"],
                    "can_delegate": True,
                },
                {
                    "agent_id": "agent.research",
                    "display_name": "Research",
                    "role": "research",
                    "parent_id": "agent.root",
                    "children": [],
                    "tools": ["read", "write", "ls"],
                    "can_delegate": False,
                },
            ],
        })

        ctx = enforcer.create_tool_context("agent.root")
        enforcer.validate_delegation(ctx, tree, "agent.research")

    def test_validate_delegation_denied(self, enforcer):
        from my_team.agent_tree import AgentTree

        tree = AgentTree.from_dict({
            "agents": [
                {
                    "agent_id": "agent.root",
                    "display_name": "Root",
                    "role": "root",
                    "parent_id": None,
                    "children": ["agent.research"],
                    "tools": ["read", "write", "ls", "delegate"],
                    "can_delegate": True,
                },
                {
                    "agent_id": "agent.research",
                    "display_name": "Research",
                    "role": "research",
                    "parent_id": "agent.root",
                    "children": ["agent.web_research"],
                    "tools": ["read", "write", "ls", "delegate"],
                    "can_delegate": True,
                },
                {
                    "agent_id": "agent.web_research",
                    "display_name": "Web",
                    "role": "web",
                    "parent_id": "agent.research",
                    "children": [],
                    "tools": ["read", "write", "ls"],
                    "can_delegate": False,
                },
            ],
        })

        ctx = enforcer.create_tool_context("agent.web_research")
        with pytest.raises(IdentityError, match="cannot delegate"):
            enforcer.validate_delegation(ctx, tree, "agent.root")


# ---------------------------------------------------------------------------
# End-to-end identity flow
# ---------------------------------------------------------------------------

class TestEndToEndIdentity:
    def test_full_identity_flow(self, enforcer):
        """Test complete identity enforcement flow."""
        # 1. System creates context for agent
        ctx = enforcer.create_tool_context("agent.research", tick=5)
        assert ctx.agent_id == "agent.research"

        # 2. Agent tries to send email — system sets sender
        email_kwargs = enforcer.wrap_email_creation(
            context=ctx,
            to=["agent.root"],
            subject="Report",
            body="Done",
            email_type=EmailType.RESULT,
            tick=5,
        )
        assert email_kwargs["from_agent"] == "agent.research"

        # 3. Agent tries to spoof sender — rejected
        with pytest.raises(SpoofedSenderError):
            enforcer.validate_sender(ctx, "agent.root")

        # 4. Agent tries to use unauthorized tool — rejected
        with pytest.raises(ToolPermissionError):
            enforcer.validate_tool_access(ctx, "web_search")

        # 5. Agent can use allowed tool
        enforcer.validate_tool_access(ctx, "read")

    def test_cannot_create_own_context(self, enforcer):
        """Agents cannot create their own ToolContext."""
        # Only the enforcer can create contexts
        # This is enforced by design — agents don't have access to
        # IdentityEnforcer.create_tool_context() in their runtime
        ctx = enforcer.create_tool_context("agent.research")
        assert ctx.agent_id == "agent.research"

        # Even if someone tries to create a context with a different agent_id,
        # the enforcer validates against registered agents
        with pytest.raises(IdentityError):
            enforcer.create_tool_context("agent.nonexistent")
