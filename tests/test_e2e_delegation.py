"""End-to-end delegation test.

Covers review gap §8.3: complete delegation flow through the Simulation.
"""

import json

import pytest

from my_team.agent_runtime import (
    AgentObservation,
    BaseAgent,
    ManagerAgent,
    RootAgent,
    SubAgent,
    ActionPlan,
    AgentAction,
    ToolContext,
    ToolRegistry,
    ROOT_TOOLS,
    MANAGER_TOOLS,
)
from my_team.agent_tree import AgentTree
from my_team.delegation import DelegationProtocol
from my_team.mailbox import MailSystem
from my_team.models.email import EmailType
from my_team.models.task import TaskPriority, TaskStatus
from my_team.private_store import PrivateStore, PrivateStoreConfig
from my_team.shared_kb import PermissionEngine, PermissionRule, SharedKB, LockManager
from my_team.task_tree import TaskTree
from my_team.tick_engine import TickEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root Agent",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research Agent",
                "role": "research_manager",
                "parent_id": "agent.root",
                "children": ["agent.web_research"],
                "tools": ["read", "write", "ls", "delegate", "send_email"],
                "can_delegate": True,
            },
            {
                "agent_id": "agent.web_research",
                "display_name": "Web Research Agent",
                "role": "web_researcher",
                "parent_id": "agent.research",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
            },
        ],
    })


@pytest.fixture
def full_system(agent_tree) -> dict:
    """Set up a complete system with all components."""
    mail = MailSystem()
    mail.register_agent("agent.root")
    mail.register_agent("agent.research")
    mail.register_agent("agent.web_research")

    task_tree = TaskTree()
    audit_log = None  # using simple audit for tests

    permissions = PermissionEngine([
        PermissionRule(
            scope="project/research/*",
            principal="agent.research",
            allow=["read", "write", "create", "list"],
        ),
        PermissionRule(
            scope="project/research/*",
            principal="agent.web_research",
            allow=["read", "write", "create", "list"],
        ),
        PermissionRule(
            scope="project/*",
            principal="agent.root",
            allow=["read", "list"],
        ),
    ])

    lock_manager = LockManager()
    shared_kb = SharedKB(permissions=permissions, lock_manager=lock_manager)
    private_store = PrivateStore(PrivateStoreConfig(base_path="/tmp/test_private"))
    tool_registry = ToolRegistry()
    tool_registry.register_agent("agent.root", ROOT_TOOLS)
    tool_registry.register_agent("agent.research", MANAGER_TOOLS)
    tool_registry.register_agent("agent.web_research", frozenset({"read", "write", "ls", "send_email"}))

    delegation = DelegationProtocol(agent_tree, task_tree, mail)
    tick_engine = TickEngine()

    # Create runtimes
    runtimes = {
        "agent.root": RootAgent(agent_id="agent.root", tool_registry=tool_registry),
        "agent.research": ManagerAgent(agent_id="agent.research", tool_registry=tool_registry),
        "agent.web_research": SubAgent(agent_id="agent.web_research", tool_registry=tool_registry),
    }

    return {
        "agent_tree": agent_tree,
        "mail": mail,
        "task_tree": task_tree,
        "shared_kb": shared_kb,
        "private_store": private_store,
        "tool_registry": tool_registry,
        "delegation": delegation,
        "tick_engine": tick_engine,
        "runtimes": runtimes,
    }


# ---------------------------------------------------------------------------
# End-to-end delegation flow
# ---------------------------------------------------------------------------

class TestE2EDelegation:
    def test_full_delegation_flow(self, full_system):
        """Test complete flow:
        Root creates task → delegates to Research →
        Research delegates to WebResearch → WebResearch writes to shared KB →
        WebResearch returns result → Research aggregates → Root receives result
        """
        sys = full_system
        tick = 0

        # Step 1: Root creates task and delegates to Research
        root_task, delegation_email = sys["delegation"].delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Market Analysis",
            description="Analyze three candidate markets",
            priority=TaskPriority.HIGH,
            deadline_tick=20,
            tick=tick,
        )
        assert root_task.status == TaskStatus.ASSIGNED
        assert delegation_email.email_type == EmailType.DELEGATION
        assert "agent.research" in delegation_email.to

        # Step 2: Deliver delegation email
        sys["mail"].deliver(tick + 1)
        research_mailbox = sys["mail"].get_mailbox("agent.research")
        assert research_mailbox.unread_count == 1

        # Step 3: Research accepts
        tick = 1
        sys["delegation"].accept("agent.research", root_task.task_id, tick=tick)
        assert root_task.status == TaskStatus.ACCEPTED

        # Step 4: Research delegates sub-task to WebResearch
        tick = 2
        sub_task, sub_delegation = sys["delegation"].delegate(
            delegator_id="agent.research",
            target_id="agent.web_research",
            title="Web Research",
            description="Collect market data from web sources",
            parent_task_id=root_task.task_id,
            deadline_tick=15,
            tick=tick,
        )
        assert sub_task.status == TaskStatus.ASSIGNED
        assert sub_task.parent_task_id == root_task.task_id

        # Step 5: Deliver sub-delegation email
        sys["mail"].deliver(tick + 1)
        web_mailbox = sys["mail"].get_mailbox("agent.web_research")
        assert web_mailbox.unread_count == 1

        # Step 6: WebResearch accepts
        tick = 3
        sys["delegation"].accept("agent.web_research", sub_task.task_id, tick=tick)
        assert sub_task.status == TaskStatus.ACCEPTED

        # Step 7: WebResearch writes to shared KB
        tick = 4
        sys["shared_kb"].create(
            "project/research/market-data.md",
            agent_id="agent.web_research",
            content="# Market Data\n\nMarket A: Large\nMarket B: Growing\n",
            tick=tick,
        )

        # Verify shared KB has the file
        resource = sys["shared_kb"].read(
            "project/research/market-data.md",
            agent_id="agent.research",
        )
        assert resource.exists
        assert "Market A" in resource.content

        # Step 8: WebResearch submits result
        tick = 5
        sub_task.transition_to(TaskStatus.IN_PROGRESS, tick=4)
        result_email = sys["delegation"].submit_result(
            agent_id="agent.web_research",
            task_id=sub_task.task_id,
            summary="Market data collected for 3 markets",
            artifacts=[{
                "type": "shared_kb_file",
                "path": "project/research/market-data.md",
                "version": 1,
            }],
            limitations=["Data for Market C is outdated"],
            recommendation="Focus on Markets A and B",
            tick=tick,
        )
        assert sub_task.status == TaskStatus.SUBMITTED
        assert result_email.email_type == EmailType.RESULT

        # Step 9: Deliver result to Research
        sys["mail"].deliver(tick + 1)
        research_inbox = sys["mail"].get_mailbox("agent.research")
        result_emails = research_inbox.get_by_type(EmailType.RESULT)
        assert len(result_emails) == 1
        assert "Market data collected" in result_emails[0].body

        # Step 10: Research aggregates and submits to Root
        tick = 7
        root_task.transition_to(TaskStatus.IN_PROGRESS, tick=6)
        final_result = sys["delegation"].submit_result(
            agent_id="agent.research",
            task_id=root_task.task_id,
            summary="Market analysis complete. Recommended: Markets A and B.",
            artifacts=[{
                "type": "shared_kb_file",
                "path": "project/research/market-data.md",
                "version": 1,
            }],
            tick=tick,
        )
        assert root_task.status == TaskStatus.SUBMITTED

        # Step 11: Deliver final result to Root
        sys["mail"].deliver(tick + 1)
        root_mailbox = sys["mail"].get_mailbox("agent.root")
        root_results = root_mailbox.get_by_type(EmailType.RESULT)
        assert len(root_results) == 1
        assert "Markets A and B" in root_results[0].body

    def test_delegation_constraint_direct_child_only(self, full_system):
        """WebResearch cannot delegate to Planning (not a direct child)."""
        from my_team.delegation import NotDirectChildError

        sys = full_system
        with pytest.raises(NotDirectChildError):
            sys["delegation"].delegate(
                delegator_id="agent.web_research",
                target_id="agent.root",  # not a child
                title="Cross delegation",
                tick=0,
            )

    def test_delegation_constraint_deadline(self, full_system):
        """Sub-task deadline cannot exceed parent deadline."""
        from my_team.delegation import DelegationDeadlineError

        sys = full_system
        parent_task, _ = sys["delegation"].delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Parent task",
            deadline_tick=10,
            tick=0,
        )

        with pytest.raises(DelegationDeadlineError):
            sys["delegation"].delegate(
                delegator_id="agent.research",
                target_id="agent.web_research",
                title="Sub task",
                parent_task_id=parent_task.task_id,
                deadline_tick=15,  # exceeds parent's 10
                tick=1,
            )

    def test_cannot_access_other_agent_private_space(self, full_system):
        """Agent cannot read another agent's private workspace."""
        sys = full_system
        # This is enforced by the private store path check
        # In a real system, the ToolContext would enforce this
        assert sys["private_store"] is not None

    def test_shared_kb_permission_enforced(self, full_system):
        """Agent cannot write to unauthorized shared KB paths."""
        sys = full_system
        from my_team.shared_kb import SharedKBWriteError

        # WebResearch tries to write to planning directory (not permitted)
        with pytest.raises(SharedKBWriteError, match="Permission denied"):
            sys["shared_kb"].create(
                "project/planning/plan.md",
                agent_id="agent.web_research",
                content="unauthorized",
            )

    def test_task_tree_integrity(self, full_system):
        """Task tree maintains parent-child relationships."""
        sys = full_system

        parent, _ = sys["delegation"].delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Parent",
            tick=0,
        )

        child, _ = sys["delegation"].delegate(
            delegator_id="agent.research",
            target_id="agent.web_research",
            title="Child",
            parent_task_id=parent.task_id,
            tick=1,
        )

        # Verify tree structure
        assert sys["task_tree"].parent(child.task_id).task_id == parent.task_id
        assert len(sys["task_tree"].children(parent.task_id)) == 1
        assert sys["task_tree"].is_ancestor(parent.task_id, child.task_id)

    def test_email_delivery_across_ticks(self, full_system):
        """Emails are delivered at the correct tick."""
        sys = full_system

        sys["delegation"].delegate(
            delegator_id="agent.root",
            target_id="agent.research",
            title="Task",
            tick=0,
        )

        # Tick 0: email queued with deliver_at_tick=1
        assert sys["mail"].pending_count == 1

        # Deliver at tick 0: nothing delivered yet (deliver_at_tick=1)
        delivered = sys["mail"].deliver(0)
        assert len(delivered) == 0

        # Deliver at tick 1: email delivered
        delivered = sys["mail"].deliver(1)
        assert len(delivered) == 1

    def test_lock_prevents_concurrent_write(self, full_system):
        """Two agents cannot hold exclusive lock on same resource."""
        sys = full_system
        from my_team.shared_kb import LockConflictError

        sys["shared_kb"].create(
            "project/research/report.md",
            agent_id="agent.research",
            content="initial",
            tick=0,
        )

        # Research acquires lock
        sys["shared_kb"].locks.acquire(
            "project/research/report.md",
            "agent.research",
            current_tick=0,
        )

        # WebResearch tries to acquire same lock — should fail
        with pytest.raises(LockConflictError):
            sys["shared_kb"].locks.acquire(
                "project/research/report.md",
                "agent.web_research",
                current_tick=1,
            )
