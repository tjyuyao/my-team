"""End-to-end tests for Shared KB write through the commit pipeline.

Proves the full KB write path:
  Agent stages KB_WRITE effect (via tool handler or TransactionBuffer)
  → Phase 6 validation (permission, lock, version)
  → Phase 8 commit applies via SharedKB._apply_committed()
  → version increments, content visible

Also verifies the single-write-entry refactor:
  - SharedKB.write() is renamed to _apply_committed() (internal)
  - Agents stage effects; the commit pipeline applies them
"""

from __future__ import annotations

import pytest

from my_team.agent_runtime import (
    ActionPlan,
    AgentAction,
    BaseAgent,
    action_plan_to_intents,
)
from my_team.agent_tree import AgentTree
from my_team.shared_kb import PermissionRule, SharedKBWriteError
from my_team.simulation import Simulation
from my_team.transaction import EffectStatus, EffectType


def _make_kb_sim() -> Simulation:
    """Simulation with a shared KB and permission rules."""
    tree = AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate", "kb_write"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })
    sim = Simulation(agent_tree=tree)
    # Grant KB write permission
    sim._permission_engine.add_rules([
        PermissionRule(
            scope="project/research/*",
            principal="agent.root",
            allow=["read", "create", "write", "kb_write", "lock", "unlock"],
        ),
    ])
    return sim


class TestKBSingleWriteEntry:
    """SharedKB write is now internal-only; agents stage effects."""

    def test_write_renamed_to_apply_committed(self) -> None:
        """SharedKB.write() no longer exists — only _apply_committed()."""
        from my_team.shared_kb import SharedKB
        assert not hasattr(SharedKB, "write")
        assert hasattr(SharedKB, "_apply_committed")

    def test_staged_kb_write_commits(self) -> None:
        """KB_WRITE effect staged → committed → content + version updated."""
        sim = _make_kb_sim()

        # Acquire lock on the resource
        lock = sim._lock_manager.acquire(
            "project/research/report.md", "agent.root", current_tick=0,
        )

        # Stage a KB_WRITE effect directly
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.root",
            "project/research/report.md",
            data={"content": "v1 content", "expected_version": 0},
            expected_version=0,
            lock_token=lock.lock_token,
        )
        sim._phase_commit(0, {})

        # Resource created with version 1
        resource = sim._shared_kb.read(
            "project/research/report.md", "agent.root",
        )
        assert resource.content == "v1 content"
        assert resource.version == 1

    def test_kb_write_via_tool_handler(self) -> None:
        """kb_write tool handler stages the effect through the registry."""
        sim = _make_kb_sim()
        sim._lock_manager.acquire(
            "project/research/report.md", "agent.root", current_tick=0,
        )

        # Agent produces a kb_write action through the intent pipeline
        class KBAgent(BaseAgent):
            def decide_intents(self, observation, continuation=None):
                plan = ActionPlan(
                    agent_id="agent.root",
                    tick=observation.tick,
                    actions=[AgentAction(
                        action_type="kb_write",
                        tool_name="kb_write",
                        payload={
                            "path": "project/research/report.md",
                            "content": "via tool handler",
                            "expected_version": 0,
                        },
                    )],
                )
                return action_plan_to_intents(plan)

        agent = KBAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent
        sim.run_tick()

        resource = sim._shared_kb.read(
            "project/research/report.md", "agent.root",
        )
        assert resource.content == "via tool handler"
        assert resource.version == 1

    def test_kb_write_rejected_without_lock(self) -> None:
        """KB write without a lock is rejected at commit validation."""
        sim = _make_kb_sim()
        # No lock acquired

        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.root",
            "project/research/report.md",
            data={"content": "no lock", "expected_version": 0},
            expected_version=0,
        )
        sim._phase_commit(0, {})

        # Effect should be FAILED, not applied
        effects = list(sim._transaction_buffer._effects.values())
        assert effects[0].status == EffectStatus.FAILED
        # Resource not created
        with pytest.raises(SharedKBWriteError):
            sim._shared_kb.read("project/research/report.md", "agent.root")

    def test_kb_write_rejected_wrong_version(self) -> None:
        """KB write with stale version is rejected."""
        sim = _make_kb_sim()
        lock = sim._lock_manager.acquire(
            "project/research/report.md", "agent.root", current_tick=0,
        )

        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.root",
            "project/research/report.md",
            data={"content": "stale version", "expected_version": 5},
            expected_version=5,  # wrong — current is 0
            lock_token=lock.lock_token,
        )
        sim._phase_commit(0, {})

        effects = list(sim._transaction_buffer._effects.values())
        assert effects[0].status == EffectStatus.FAILED
        assert "Version" in (effects[0].error or "")

    def test_kb_write_rejected_wrong_lock_token(self) -> None:
        """KB write with forged lock token is rejected."""
        sim = _make_kb_sim()
        sim._lock_manager.acquire(
            "project/research/report.md", "agent.root", current_tick=0,
        )

        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.root",
            "project/research/report.md",
            data={"content": "forged", "expected_version": 0},
            expected_version=0,
            lock_token="forged_token",
        )
        sim._phase_commit(0, {})

        effects = list(sim._transaction_buffer._effects.values())
        assert effects[0].status == EffectStatus.FAILED

    def test_kb_write_version_increments(self) -> None:
        """Sequential writes increment the version."""
        sim = _make_kb_sim()
        lock = sim._lock_manager.acquire(
            "project/research/report.md", "agent.root", current_tick=0,
        )

        # Write 1
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.root",
            "project/research/report.md",
            data={"content": "first", "expected_version": 0},
            expected_version=0,
            lock_token=lock.lock_token,
        )
        sim._phase_commit(0, {})
        sim._transaction_buffer.clear()

        # Write 2 — must use updated version
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.root",
            "project/research/report.md",
            data={"content": "second", "expected_version": 1},
            expected_version=1,
            lock_token=lock.lock_token,
        )
        sim._phase_commit(0, {})
        sim._transaction_buffer.clear()

        resource = sim._shared_kb.read(
            "project/research/report.md", "agent.root",
        )
        assert resource.content == "second"
        assert resource.version == 2

    def test_direct_apply_bypasses_validation_but_is_internal(self) -> None:
        """_apply_committed still enforces permission/lock/version itself."""
        sim = _make_kb_sim()
        # No lock — internal apply should still reject
        with pytest.raises(SharedKBWriteError, match="Must hold lock"):
            sim._shared_kb._apply_committed(
                path="project/research/report.md",
                agent_id="agent.root",
                content="direct",
                expected_version=0,
            )
