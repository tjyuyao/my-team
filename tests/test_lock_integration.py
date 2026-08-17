"""Integration tests for mutex lock correctness in the commit pipeline.

Verifies the lock lifecycle through TransactionBuffer + LockManager +
Simulation._phase_commit:

- Same-tick contention: two agents write the same shared KB resource
- Lock acquire → release → re-acquire across ticks
- Write rejected without lock
- Lock token mismatch rejected
"""

from __future__ import annotations

from my_team.agent_tree import AgentTree
from my_team.shared_kb import LockManager, PermissionEngine, SharedKB
from my_team.simulation import Simulation
from my_team.transaction import EffectStatus, EffectType, TransactionBuffer


def _make_kb() -> tuple[SharedKB, PermissionEngine, LockManager]:
    """Create a SharedKB with a lock manager."""
    permissions = PermissionEngine()
    locks = LockManager(default_lease_ticks=4)
    kb = SharedKB(permissions=permissions, lock_manager=locks)
    return kb, permissions, locks


def _make_sim_with_kb() -> Simulation:
    """Create a simulation with a single agent tree."""
    tree = AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
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
    return Simulation(agent_tree=tree)


class TestSameTickContention:
    """Two agents write the same shared KB resource in the same tick."""

    def test_lock_holder_wins_non_holder_rejected(self) -> None:
        """Agent A holds the lock; A's write passes, B's write fails."""
        kb, _, locks = _make_kb()
        locks.acquire("project/research/report.md", "agent.a", current_tick=0)
        lock = locks.get_lock("project/research/report.md")

        buf = TransactionBuffer()
        buf.stage(
            EffectType.KB_WRITE,
            "agent.a",
            "project/research/report.md",
            data={"content": "A version"},
            lock_token=lock.lock_token,
        )
        buf.stage(
            EffectType.KB_WRITE,
            "agent.b",
            "project/research/report.md",
            data={"content": "B version"},
            lock_token="wrong_token",
        )

        buf.validate(check_lock=lambda r, a, t: (
            locks.get_lock(r) is not None
            and locks.get_lock(r).owner_agent_id == a
            and (t is None or locks.get_lock(r).lock_token == t)
        ))

        # A's effect passes (holds lock with correct token)
        effects = buf.get_effects()
        a_effect = next(e for e in effects if e.agent_id == "agent.a")
        b_effect = next(e for e in effects if e.agent_id == "agent.b")
        assert a_effect.status == EffectStatus.VALIDATED
        # B's effect fails (doesn't hold lock)
        assert b_effect.status == EffectStatus.FAILED

    def test_no_lock_no_write(self) -> None:
        """A write effect without a lock is rejected at commit-time validation."""
        kb, _, locks = _make_kb()
        # No lock acquired

        buf = TransactionBuffer()
        buf.stage(
            EffectType.KB_WRITE,
            "agent.a",
            "project/research/report.md",
            data={"content": "unauthorized write"},
            lock_token=None,
        )

        failures = buf.validate(check_lock=lambda r, a, t: (
            locks.get_lock(r) is not None
            and locks.get_lock(r).owner_agent_id == a
        ))
        assert len(failures) == 1
        assert "Must hold lock" in failures[0].error

    def test_commit_respects_lock_holder(self) -> None:
        """Full pipeline: only the lock holder's effect is committed."""
        sim = _make_sim_with_kb()
        locks = sim._lock_manager

        # Grant both agents write permission on the shared KB path
        from my_team.shared_kb import PermissionRule
        sim._permission_engine.add_rules([
            PermissionRule(
                scope="project/research/*",
                principal="agent.a",
                allow=["read", "write", "kb_write"],
            ),
            PermissionRule(
                scope="project/research/*",
                principal="agent.b",
                allow=["read", "write", "kb_write"],
            ),
        ])

        # Agent A holds the lock on a shared KB resource
        locks.acquire("project/research/report.md", "agent.a", current_tick=0)
        lock = locks.get_lock("project/research/report.md")

        # Stage two competing effects via the transaction buffer
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.a",
            "project/research/report.md",
            data={"content": "A"},
            lock_token=lock.lock_token,
        )
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.b",
            "project/research/report.md",
            data={"content": "B"},
            lock_token="forged",
        )

        # Run the commit phase
        sim._phase_commit(0, {})
        buffer = sim._transaction_buffer

        # Only A's effect should be committed
        a_effect = next(e for e in buffer._effects.values() if e.agent_id == "agent.a")
        b_effect = next(e for e in buffer._effects.values() if e.agent_id == "agent.b")
        assert a_effect.status == EffectStatus.COMMITTED
        assert b_effect.status == EffectStatus.FAILED


class TestLockAcrossTicks:
    """Lock acquire → release → re-acquire across ticks."""

    def test_lock_release_allows_reacquire(self) -> None:
        """After A releases, B can acquire in a later tick."""
        kb, _, locks = _make_kb()
        lock_a = locks.acquire("project/research/report.md", "agent.a", current_tick=0)

        # A writes and releases
        locks.release("project/research/report.md", "agent.a", lock_a.lock_token)
        assert not locks.is_locked("project/research/report.md")

        # B acquires in tick 2
        lock_b = locks.acquire("project/research/report.md", "agent.b", current_tick=2)
        assert lock_b.owner_agent_id == "agent.b"
        assert locks.is_locked("project/research/report.md")

    def test_expired_lock_allows_reacquire(self) -> None:
        """If A's lease expires without release, B can acquire."""
        kb, _, locks = _make_kb()
        locks.acquire("project/research/report.md", "agent.a", current_tick=0, lease_ticks=2)

        # Lease expires at tick 3
        expired = locks.check_expired(current_tick=3)
        assert len(expired) == 1

        # B acquires
        lock_b = locks.acquire("project/research/report.md", "agent.b", current_tick=3)
        assert lock_b.owner_agent_id == "agent.b"

    def test_write_requires_lock_each_commit(self) -> None:
        """A write without a lock fails at commit-time validation even if
        the lock was held in a previous tick."""
        sim = _make_sim_with_kb()
        locks = sim._lock_manager

        # Tick 0: A holds the lock
        lock = locks.acquire("project/research/report.md", "agent.a", current_tick=0)

        # Tick 4: A's lease expired (default lease = 4 ticks), lock is gone
        expired = locks.check_expired(current_tick=4)
        assert len(expired) == 1
        assert locks.get_lock("project/research/report.md") is None

        # A tries to write without re-acquiring → rejected
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.a",
            "project/research/report.md",
            data={"content": "stale write"},
            lock_token=lock.lock_token,  # stale token from previous lease
        )
        sim._phase_commit(4, {})
        effects = list(sim._transaction_buffer._effects.values())
        assert effects[0].status == EffectStatus.FAILED


class TestLockTokenVerification:
    """Lock token correctness."""

    def test_wrong_token_rejected(self) -> None:
        """A write with the wrong lock_token is rejected."""
        kb, _, locks = _make_kb()
        locks.acquire("project/research/report.md", "agent.a", current_tick=0)

        buf = TransactionBuffer()
        buf.stage(
            EffectType.KB_WRITE,
            "agent.a",  # correct owner
            "project/research/report.md",
            data={"content": "bad token"},
            lock_token="forged_token",
        )

        failures = buf.validate(check_lock=lambda r, a, t: (
            locks.get_lock(r) is not None
            and locks.get_lock(r).owner_agent_id == a
            and locks.get_lock(r).lock_token == t
        ))
        assert len(failures) == 1
        assert "Must hold lock" in failures[0].error

    def test_correct_token_passes(self) -> None:
        """A write with the correct lock_token passes validation."""
        kb, _, locks = _make_kb()
        lock = locks.acquire("project/research/report.md", "agent.a", current_tick=0)

        buf = TransactionBuffer()
        buf.stage(
            EffectType.KB_WRITE,
            "agent.a",
            "project/research/report.md",
            data={"content": "good token"},
            lock_token=lock.lock_token,
        )

        failures = buf.validate(check_lock=lambda r, a, t: (
            locks.get_lock(r) is not None
            and locks.get_lock(r).owner_agent_id == a
            and locks.get_lock(r).lock_token == t
        ))
        assert len(failures) == 0

    def test_simulation_check_lock_verifies_token(self) -> None:
        """Simulation._phase_commit's check_lock verifies lock_token."""
        sim = _make_sim_with_kb()
        locks = sim._lock_manager
        locks.acquire("project/research/report.md", "agent.a", current_tick=0)

        # Stale token effect → should be rejected by simulation's check_lock
        sim._transaction_buffer.stage(
            EffectType.KB_WRITE,
            "agent.a",
            "project/research/report.md",
            data={"content": "stale"},
            lock_token="forged",
        )
        sim._phase_commit(0, {})
        effects = list(sim._transaction_buffer._effects.values())
        assert effects[0].status == EffectStatus.FAILED


class TestPrivateWorkspaceLockExemption:
    """Private workspace writes are exempt from lock checks."""

    def test_private_write_no_lock_required(self) -> None:
        """A private workspace write does NOT need a lock."""
        sim = _make_sim_with_kb()

        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE,
            "agent.root",
            "notes.md",
            data={"content": "private note"},
        )
        sim._phase_commit(0, {})
        effects = list(sim._transaction_buffer._effects.values())
        # Private write should pass validation (no lock needed)
        assert effects[0].status == EffectStatus.COMMITTED
