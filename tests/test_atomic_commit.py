"""Tests for atomic commit transaction buffer.

Covers review gap §8.4 (atomic commit).
"""

import pytest

from my_team.transaction import (
    ConflictResolution,
    EffectStatus,
    EffectType,
    StagedEffect,
    TransactionBuffer,
)


# ---------------------------------------------------------------------------
# Staging effects
# ---------------------------------------------------------------------------

class TestStaging:
    def test_stage_effect(self):
        buf = TransactionBuffer()
        effect = buf.stage(
            effect_type=EffectType.KB_WRITE,
            agent_id="agent.a",
            resource="report.md",
            data={"content": "hello"},
        )
        assert effect.effect_type == EffectType.KB_WRITE
        assert effect.agent_id == "agent.a"
        assert effect.status == EffectStatus.STAGED

    def test_stage_with_version(self):
        buf = TransactionBuffer()
        effect = buf.stage(
            effect_type=EffectType.KB_WRITE,
            agent_id="agent.a",
            resource="report.md",
            expected_version=3,
        )
        assert effect.expected_version == 3

    def test_unique_effect_ids(self):
        buf = TransactionBuffer()
        e1 = buf.stage(EffectType.KB_WRITE, "a", "r1")
        e2 = buf.stage(EffectType.KB_WRITE, "a", "r2")
        assert e1.effect_id != e2.effect_id

    def test_get_effects_by_agent(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "r1")
        buf.stage(EffectType.KB_WRITE, "agent.b", "r2")
        buf.stage(EffectType.KB_WRITE, "agent.a", "r3")

        a_effects = buf.get_effects(agent_id="agent.a")
        assert len(a_effects) == 2

    def test_get_effects_for_resource(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
        buf.stage(EffectType.KB_WRITE, "agent.b", "report.md")
        buf.stage(EffectType.KB_WRITE, "agent.a", "other.md")

        effects = buf.get_effects_for_resource("report.md")
        assert len(effects) == 2


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_validate_version_pass(self):
        buf = TransactionBuffer()
        buf.stage(
            EffectType.KB_WRITE, "agent.a", "report.md",
            expected_version=1,
        )

        def check_version(resource: str, expected: int) -> bool:
            return True  # version matches

        failures = buf.validate(check_version=check_version)
        assert len(failures) == 0

    def test_validate_version_fail(self):
        buf = TransactionBuffer()
        effect = buf.stage(
            EffectType.KB_WRITE, "agent.a", "report.md",
            expected_version=1,
        )

        def check_version(resource: str, expected: int) -> bool:
            return False  # version mismatch

        failures = buf.validate(check_version=check_version)
        assert len(failures) == 1
        assert failures[0].status == EffectStatus.FAILED
        assert "Version conflict" in failures[0].error

    def test_validate_lock_fail(self):
        buf = TransactionBuffer()
        effect = buf.stage(
            EffectType.KB_WRITE, "agent.a", "report.md",
        )

        def check_lock(resource: str, agent_id: str) -> bool:
            return False  # no lock held

        failures = buf.validate(check_lock=check_lock)
        assert len(failures) == 1
        assert "Must hold lock" in failures[0].error

    def test_validate_permission_fail(self):
        buf = TransactionBuffer()
        effect = buf.stage(
            EffectType.KB_WRITE, "agent.a", "report.md",
        )

        def check_permission(agent_id: str, resource: str, op: str) -> bool:
            return False  # no permission

        failures = buf.validate(check_permission=check_permission)
        assert len(failures) == 1
        assert "Permission denied" in failures[0].error

    def test_validate_passes_all(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md", expected_version=1)

        failures = buf.validate(
            check_version=lambda r, v: True,
            check_lock=lambda r, a: True,
            check_permission=lambda a, r, o: True,
        )
        assert len(failures) == 0
        # Effect should be validated
        effects = buf.get_effects()
        assert effects[0].status == EffectStatus.VALIDATED


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------

class TestConflictResolution:
    def test_no_conflicts_single_effect(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
        buf.validate()
        resolutions = buf.resolve_conflicts()
        assert len(resolutions) == 0

    def test_conflict_same_resource(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
        buf.stage(EffectType.KB_WRITE, "agent.b", "report.md")
        buf.validate()

        resolutions = buf.resolve_conflicts()
        assert len(resolutions) == 1

        # Winner should be agent.a (alphabetically first)
        assert resolutions[0].winner.startswith("eff.")
        assert len(resolutions[0].losers) == 1

    def test_winner_deterministic(self):
        """Same agents, same resource → always same winner."""
        winners = []
        for _ in range(10):
            buf = TransactionBuffer()
            e1 = buf.stage(EffectType.KB_WRITE, "agent.z", "report.md")
            e2 = buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
            buf.validate()
            resolutions = buf.resolve_conflicts()
            winners.append(resolutions[0].winner)

        # All winners should be the same effect
        assert len(set(winners)) == 1

    def test_different_resources_no_conflict(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
        buf.stage(EffectType.KB_WRITE, "agent.b", "plan.md")
        buf.validate()

        resolutions = buf.resolve_conflicts()
        assert len(resolutions) == 0


# ---------------------------------------------------------------------------
# Commit and rollback
# ---------------------------------------------------------------------------

class TestCommitRollback:
    def test_commit_validated_effects(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
        buf.stage(EffectType.KB_WRITE, "agent.b", "plan.md")
        buf.validate()

        committed = buf.commit()
        assert len(committed) == 2
        assert all(e.status == EffectStatus.COMMITTED for e in committed)
        assert buf.committed_count == 2

    def test_commit_skips_failed(self):
        buf = TransactionBuffer()
        e1 = buf.stage(EffectType.KB_WRITE, "agent.a", "report.md", expected_version=1)
        e2 = buf.stage(EffectType.KB_WRITE, "agent.b", "plan.md")

        # e1 fails version check
        buf.validate(check_version=lambda r, v: r != "report.md")
        committed = buf.commit()
        assert len(committed) == 1
        assert committed[0].resource == "plan.md"

    def test_rollback(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
        buf.validate()
        buf.commit()

        rolled_back = buf.rollback()
        assert len(rolled_back) == 1
        assert rolled_back[0].status == EffectStatus.ROLLED_BACK
        assert buf.committed_count == 0

    def test_clear(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
        buf.validate()
        buf.commit()
        buf.clear()
        assert buf.summary()["total_effects"] == 0

    def test_summary(self):
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.a", "r1", expected_version=1)
        buf.stage(EffectType.KB_WRITE, "agent.b", "r2", expected_version=2)
        buf.validate(check_version=lambda r, v: r != "r2")

        summary = buf.summary()
        assert summary["total_effects"] == 2
        assert summary["validated"] == 1
        assert summary["failed"] == 1


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------

class TestDeterministicOrdering:
    def test_same_resource_same_agent(self):
        """Multiple effects from same agent on same resource: deterministic order."""
        buf = TransactionBuffer()
        e1 = buf.stage(EffectType.KB_WRITE, "agent.a", "report.md", data={"v": 1})
        e2 = buf.stage(EffectType.KB_WRITE, "agent.a", "report.md", data={"v": 2})
        buf.validate()

        resolutions = buf.resolve_conflicts()
        assert len(resolutions) == 1

        # Deterministic: sorted by effect_id, so e1 wins (earlier ID)
        winner_id = resolutions[0].winner
        assert winner_id == e1.effect_id

    def test_three_agents_same_resource(self):
        """Three agents competing for same resource."""
        buf = TransactionBuffer()
        buf.stage(EffectType.KB_WRITE, "agent.c", "report.md")
        buf.stage(EffectType.KB_WRITE, "agent.a", "report.md")
        buf.stage(EffectType.KB_WRITE, "agent.b", "report.md")
        buf.validate()

        resolutions = buf.resolve_conflicts()
        assert len(resolutions) == 1
        assert len(resolutions[0].losers) == 2

        # Winner should be agent.a (alphabetically first)
        winner = buf.get_effects()[0]
        for e in buf.get_effects():
            if e.effect_id == resolutions[0].winner:
                winner = e
                break
        assert winner.agent_id == "agent.a"
