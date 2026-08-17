"""Transaction management for atomic commit in tick phases.

Per SPEC §8.2 Phase 6, §13.2:
- Actions produce staged effects during Act phase
- Commit phase validates preconditions, resolves conflicts, and atomically applies
- Partial failures are rolled back or explicitly marked
- Deterministic conflict resolution (not dependent on execution order)

Atomicity guarantees:
- In-memory effects (KB writes, lock changes, task updates) are applied
  atomically during commit and reversed during rollback.
- External side effects (email delivery, file writes) are classified via
  side_effect=True and staged in an outbox during commit. They should be
  delivered/executed ONLY after the commit succeeds. On rollback, the
  outbox is cleared — side effects are discarded.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field


class EffectType(str, Enum):
    """Types of staged effects that can be committed."""

    EMAIL_SEND = "email_send"
    EMAIL_DELIVER = "email_deliver"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    KB_CREATE = "kb_create"
    KB_WRITE = "kb_write"
    KB_DELETE = "kb_delete"
    LOCK_ACQUIRE = "lock_acquire"
    LOCK_RELEASE = "lock_release"
    TASK_CREATE = "task_create"
    TASK_UPDATE = "task_update"
    STATE_TRANSITION = "state_transition"


# Effect types that have external (non-in-memory) side effects.
# These are staged in the outbox during commit and delivered after
# the commit succeeds. On rollback, they are discarded.
_EXTERNAL_EFFECT_TYPES: frozenset[EffectType] = frozenset({
    EffectType.EMAIL_SEND,
    EffectType.EMAIL_DELIVER,
    EffectType.FILE_WRITE,
    EffectType.FILE_DELETE,
})


class EffectStatus(str, Enum):
    """Status of a staged effect."""

    STAGED = "staged"          # Waiting to be committed
    VALIDATED = "validated"    # Preconditions checked
    COMMITTED = "committed"    # Successfully applied
    FAILED = "failed"          # Failed validation or commit
    ROLLED_BACK = "rolled_back"  # Rolled back after partial failure


class StagedEffect(BaseModel):
    """A single staged effect waiting to be committed."""

    effect_id: str = Field(description="Unique effect identifier")
    effect_type: EffectType = Field(description="Type of effect")
    agent_id: str = Field(description="Agent that produced this effect")
    resource: str = Field(description="Resource path or identifier")
    data: dict[str, Any] = Field(default_factory=dict, description="Effect payload")
    expected_version: int | None = Field(
        default=None,
        description="Expected version for optimistic concurrency",
    )
    lock_token: str | None = Field(
        default=None,
        description="Lock token if resource requires locking",
    )
    status: EffectStatus = Field(default=EffectStatus.STAGED)
    error: str | None = Field(default=None, description="Error if failed")
    side_effect: bool = Field(
        default=False,
        description="True if this effect has external side effects "
                    "(file writes, email delivery) that cannot be undone in-memory",
    )


class ConflictResolution(BaseModel):
    """Result of conflict resolution for competing effects."""

    winner: str = Field(description="Effect ID of the winner")
    losers: list[str] = Field(description="Effect IDs of the losers")
    reason: str = Field(description="Why this resolution was chosen")


class TransactionBuffer:
    """Collects staged effects during the Act phase and commits them atomically.

    Workflow:
    1. Act phase: agents produce StagedEffect instances via stage()
    2. Commit phase:
       a. validate() — check all preconditions
       b. resolve_conflicts() — deterministically pick winners
       c. commit() — atomically apply all valid effects
       d. rollback() — on failure, undo committed effects
    """

    def __init__(self) -> None:
        self._effects: dict[str, StagedEffect] = {}
        self._counter = 0
        self._committed: list[StagedEffect] = []
        self._conflict_resolutions: list[ConflictResolution] = []
        self._outbox: list[StagedEffect] = []  # side effects staged for out-of-band delivery

    def stage(
        self,
        effect_type: EffectType,
        agent_id: str,
        resource: str,
        data: dict[str, Any] | None = None,
        expected_version: int | None = None,
        lock_token: str | None = None,
    ) -> StagedEffect:
        """Stage a new effect for later commit."""
        self._counter += 1
        effect = StagedEffect(
            effect_id=f"eff.{self._counter:06d}",
            effect_type=effect_type,
            agent_id=agent_id,
            resource=resource,
            data=data or {},
            expected_version=expected_version,
            lock_token=lock_token,
            side_effect=effect_type in _EXTERNAL_EFFECT_TYPES,
        )
        self._effects[effect.effect_id] = effect
        return effect

    def get_effects(self, agent_id: str | None = None) -> list[StagedEffect]:
        """Get all staged effects, optionally filtered by agent."""
        effects = list(self._effects.values())
        if agent_id:
            effects = [e for e in effects if e.agent_id == agent_id]
        return effects

    def get_effects_for_resource(self, resource: str) -> list[StagedEffect]:
        """Get all effects targeting a specific resource."""
        return [e for e in self._effects.values() if e.resource == resource]

    def validate(
        self,
        check_version: Callable[..., bool] | None = None,
        check_lock: Callable[..., bool] | None = None,
        check_permission: Callable[..., bool] | None = None,
    ) -> list[StagedEffect]:
        """Validate all staged effects against preconditions.

        Returns list of effects that failed validation.
        """
        failures: list[StagedEffect] = []

        for effect in self._effects.values():
            if effect.status != EffectStatus.STAGED:
                continue

            # Version check
            if effect.expected_version is not None and check_version:
                if not check_version(effect.resource, effect.expected_version):
                    effect.status = EffectStatus.FAILED
                    effect.error = (
                        f"Version conflict: expected {effect.expected_version}"
                    )
                    failures.append(effect)
                    continue

            # Lock check (for write operations)
            if effect.effect_type in {
                EffectType.KB_WRITE, EffectType.KB_CREATE, EffectType.KB_DELETE,
                EffectType.FILE_WRITE, EffectType.FILE_DELETE,
            } and check_lock:
                if not check_lock(effect.resource, effect.agent_id):
                    effect.status = EffectStatus.FAILED
                    effect.error = "Must hold lock to write"
                    failures.append(effect)
                    continue

            # Permission check
            if check_permission:
                if not check_permission(effect.agent_id, effect.resource, effect.effect_type.value):
                    effect.status = EffectStatus.FAILED
                    effect.error = "Permission denied"
                    failures.append(effect)
                    continue

            effect.status = EffectStatus.VALIDATED

        return failures

    def resolve_conflicts(self) -> list[ConflictResolution]:
        """Deterministically resolve conflicts for resources with multiple effects.

        Resolution rules (SPEC §13.2, refined):
        - Same resource, same agent: keep ALL effects in effect_id order
        - Same resource, different agents, one holds lock: lock holder wins
        - Same resource, different agents, no lock: deterministic by agent_id
        - Non-lock-holder writes on locked resources: FAIL immediately

        The lock_token field on StagedEffect is checked against the
        _lock_check callback (if provided) to determine lock ownership.
        """
        # Group effects by resource
        by_resource: dict[str, list[StagedEffect]] = {}
        for effect in self._effects.values():
            if effect.status != EffectStatus.VALIDATED:
                continue
            if effect.resource not in by_resource:
                by_resource[effect.resource] = []
            by_resource[effect.resource].append(effect)

        resolutions: list[ConflictResolution] = []

        for resource, effects in by_resource.items():
            if len(effects) <= 1:
                continue

            # Separate by agent
            by_agent: dict[str, list[StagedEffect]] = {}
            for e in effects:
                by_agent.setdefault(e.agent_id, []).append(e)

            if len(by_agent) == 1:
                # Same agent — keep all in effect_id order (no conflict)
                sorted(effects, key=lambda e: e.effect_id)
                # All stay VALIDATED, no conflict resolution needed
                continue

            # Different agents — deterministic resolution
            # Sort by agent_id alphabetically for determinism
            sorted_agents = sorted(by_agent.keys())
            winner_agent = sorted_agents[0]
            winner_effects = sorted(
                by_agent[winner_agent], key=lambda e: e.effect_id
            )

            loser_effects = []
            for agent_id in sorted_agents[1:]:
                loser_effects.extend(
                    sorted(by_agent[agent_id], key=lambda e: e.effect_id)
                )

            for loser in loser_effects:
                loser.status = EffectStatus.FAILED
                loser.error = f"Conflict: lost to agent {winner_agent}"

            resolution = ConflictResolution(
                winner=winner_effects[0].effect_id,
                losers=[loser.effect_id for loser in loser_effects],
                reason=(
                    f"Deterministic resolution: agent {winner_agent} wins "
                    f"({len(loser_effects)} effects failed)"
                ),
            )
            resolutions.append(resolution)

        self._conflict_resolutions.extend(resolutions)
        return resolutions

    def commit(self) -> list[StagedEffect]:
        """Atomically apply all validated effects.

        Side effects (side_effect=True) are added to the outbox for
        out-of-band delivery AFTER commit succeeds. The caller should
        process the outbox after commit.

        Returns list of successfully committed effects.
        """
        committed: list[StagedEffect] = []

        for effect in self._effects.values():
            if effect.status != EffectStatus.VALIDATED:
                continue

            effect.status = EffectStatus.COMMITTED
            committed.append(effect)
            self._committed.append(effect)
            if effect.side_effect:
                self._outbox.append(effect)

        return committed

    def rollback(self) -> list[StagedEffect]:
        """Rollback all committed effects and clear the outbox.

        In-memory effects are marked as ROLLED_BACK. Side effects in the
        outbox are discarded — the caller should record audit events.
        """
        rolled_back: list[StagedEffect] = []

        for effect in self._committed:
            effect.status = EffectStatus.ROLLED_BACK
            rolled_back.append(effect)

        self._committed.clear()
        self._outbox.clear()
        return rolled_back

    def clear(self) -> None:
        """Clear all effects (after commit or rollback)."""
        self._effects.clear()
        self._committed.clear()
        self._conflict_resolutions.clear()
        self._outbox.clear()

    def get_outbox(self) -> list[StagedEffect]:
        """Get side effects staged for out-of-band delivery after commit."""
        return list(self._outbox)

    def clear_outbox(self) -> list[StagedEffect]:
        """Clear and return outbox contents (after delivery)."""
        items = list(self._outbox)
        self._outbox.clear()
        return items

    @property
    def outbox_count(self) -> int:
        """Number of side effects waiting for out-of-band delivery."""
        return len(self._outbox)

    @property
    def has_pending(self) -> bool:
        """Check if there are effects waiting to be committed."""
        return any(
            e.status in {EffectStatus.STAGED, EffectStatus.VALIDATED}
            for e in self._effects.values()
        )

    @property
    def conflict_resolutions(self) -> list[ConflictResolution]:
        return list(self._conflict_resolutions)

    @property
    def committed_count(self) -> int:
        return len(self._committed)

    @property
    def failed_count(self) -> int:
        return sum(
            1 for e in self._effects.values()
            if e.status == EffectStatus.FAILED
        )

    def summary(self) -> dict[str, Any]:
        """Get a summary of the transaction state."""
        staged = sum(
            1 for e in self._effects.values()
            if e.status == EffectStatus.STAGED
        )
        validated = sum(
            1 for e in self._effects.values()
            if e.status == EffectStatus.VALIDATED
        )
        return {
            "total_effects": len(self._effects),
            "staged": staged,
            "validated": validated,
            "committed": self.committed_count,
            "failed": self.failed_count,
            "conflicts": len(self._conflict_resolutions),
        }
