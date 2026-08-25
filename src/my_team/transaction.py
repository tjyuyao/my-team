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
    FILE_PATCH = "file_patch"
    FILE_DELETE = "file_delete"
    KB_CREATE = "kb_create"
    KB_WRITE = "kb_write"
    KB_DELETE = "kb_delete"
    LOCK_ACQUIRE = "lock_acquire"
    LOCK_RELEASE = "lock_release"
    TASK_CREATE = "task_create"
    TASK_UPDATE = "task_update"
    STATE_TRANSITION = "state_transition"
    RECORD_UPSERT = "record_upsert"
    RECORD_DELTA = "record_delta"
    RULE_ADVANCE = "rule_advance"
    # N4-1 记忆系统 effect（归 Agent 引擎数据面）
    MEMORY_ENTRY_WRITE = "memory_entry_write"  # 新增/追加版本
    MEMORY_ENTRY_EVICT = "memory_entry_evict"  # 撤出（从 store 中移除最新版本）
    MEMORY_ENTRY_FOLD = "memory_entry_fold"  # 折叠（合并/压缩多版本）
    # N4-2 召回引擎 effect
    MEMORY_RECALL_CONFIG = "memory_recall_config"  # 策略调整：更新可控查询词（持久）
    MEMORY_RECALL = "memory_recall"  # 主动回忆：写入临时召回策略（一次性，延迟 1 tick 生效）
    # N4-4 整理模式 effect
    MEMORY_PIN = "memory_pin"  # 固定条目：并入可控查询词（防召回降级）


# Effect types that have external (non-in-memory) side effects.
# These are staged in the outbox during commit and delivered after
# the commit succeeds. On rollback, they are discarded.
_EXTERNAL_EFFECT_TYPES: frozenset[EffectType] = frozenset(
    {
        EffectType.EMAIL_SEND,
        EffectType.EMAIL_DELIVER,
        EffectType.FILE_WRITE,
        EffectType.FILE_PATCH,
        EffectType.FILE_DELETE,
    }
)


class InvertKind(str, Enum):
    """Kinds of invert (undo) operations per effect type (SPEC §3.3, T18).

    Rollback never uses state snapshots: each effect records the minimal
    prior value (invert_data) needed to reverse ITSELF (撤回语义):
    - RESTORE_PREVIOUS: record the old value; rollback writes it back
    - REMOVE_CREATED: record nothing needed; rollback deletes what was
      created (the prior state is provably absent — e.g. create() raises
      on an existing task)
    - UNREGISTER: record the registration id; rollback discards it
    - IRREVERSIBLE: cannot be undone (side effect already left the
      system); rollback marks FAILED and audits, never silently swallows
    """

    RESTORE_PREVIOUS = "restore_previous"
    REMOVE_CREATED = "remove_created"
    UNREGISTER = "unregister"
    IRREVERSIBLE = "irreversible"


class InvertSpec(BaseModel):
    """Declarative invert contract for one effect type (single source of
    truth for WHAT an effect records and HOW it is restored)."""

    kind: InvertKind
    recorded: str = Field(
        description="What invert_data records (prior-value semantics)",
    )


# Invert contract: EVERY effect type must declare how it is undone.
# The execution of these inverts lives in Simulation._phase_commit's
# single rollback entry; this table declares the contract (SPEC §3.3).
INVERT_CONTRACT: dict[EffectType, InvertSpec] = {
    EffectType.EMAIL_SEND: InvertSpec(
        kind=InvertKind.UNREGISTER,
        recorded="outbox entry_id → discard the staged entry (email never dispatched)",
    ),
    EffectType.EMAIL_DELIVER: InvertSpec(
        kind=InvertKind.IRREVERSIBLE,
        recorded="email already delivered → cannot undo; rollback marks FAILED + audits",
    ),
    EffectType.FILE_WRITE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="file_previous: old content, or None when the file did not exist",
    ),
    EffectType.FILE_PATCH: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="file_previous: old content, or None when the file did not exist",
    ),
    EffectType.FILE_DELETE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="file_previous: deleted content (None when not on disk)",
    ),
    EffectType.KB_CREATE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="kb_state_before: prior resource-or-None + version-info-or-None",
    ),
    EffectType.KB_WRITE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="kb_state_before: (resource, version-info) prior to this write",
    ),
    EffectType.KB_DELETE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="kb_state_before: resource prior to delete (delete marks exists=False)",
    ),
    EffectType.LOCK_ACQUIRE: InvertSpec(
        kind=InvertKind.REMOVE_CREATED,
        recorded="lock_token → release on rollback",
    ),
    EffectType.LOCK_RELEASE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="prior (owner, token, lease) → re-acquire on rollback",
    ),
    EffectType.TASK_CREATE: InvertSpec(
        kind=InvertKind.REMOVE_CREATED,
        recorded="no prior state (create raises on existing) → remove task + tree registrations",
    ),
    EffectType.TASK_UPDATE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="task_state_before: deep copy of the Task prior to mutation",
    ),
    EffectType.STATE_TRANSITION: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="state_before",
    ),
    EffectType.RECORD_UPSERT: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="prior record (or None) + appended ledger entry ids → restore + remove entries",
    ),
    EffectType.RULE_ADVANCE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="prev (next_run_tick, last_fired_at) → restore schedule state",
    ),
    EffectType.RECORD_DELTA: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded="prior record (or None) + appended ledger entry ids → restore + remove entries",
    ),
    # N4-1 记忆系统 effect 逆操作
    EffectType.MEMORY_ENTRY_WRITE: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded=(
            "memory_before: (entry_id, version_chain_before_write) — "
            "新增条目时 version_chain_before_write=None（逆操作=移除该条目）；"
            "追加版本时记录上一版本链（逆操作=移除最新版本）"
        ),
    ),
    EffectType.MEMORY_ENTRY_EVICT: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded=(
            "memory_before: (entry_id, evicted_version_chain) — "
            "逆操作=将被撤出的版本链重新写回 store"
        ),
    ),
    EffectType.MEMORY_ENTRY_FOLD: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded=(
            "memory_before: (entry_id, version_chain_before_fold) — 逆操作=恢复折叠前的完整版本链"
        ),
    ),
    # N4-2 召回引擎 effect 逆操作
    EffectType.MEMORY_RECALL_CONFIG: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded=(
            "recall_config_before: 更新前的 RecallConfig 快照（可控查询词列表），"
            "逆操作=恢复原查询词列表"
        ),
    ),
    EffectType.MEMORY_RECALL: InvertSpec(
        kind=InvertKind.REMOVE_CREATED,
        recorded=(
            "temp_overrides_added: 写入 recall_config.temp_overrides 的词列表；"
            "逆操作=从 temp_overrides 移除这些词（一次性，下 tick 消费后自动清空）"
        ),
    ),
    # N4-4 整理模式 effect 逆操作
    EffectType.MEMORY_PIN: InvertSpec(
        kind=InvertKind.RESTORE_PREVIOUS,
        recorded=(
            "recall_config_before: 固定前的可控查询词列表；逆操作=恢复原列表"
            "（移除 memory_pin 并入的条目标题/触发器词）"
        ),
    ),
}


class EffectStatus(str, Enum):
    """Status of a staged effect."""

    STAGED = "staged"  # Waiting to be committed
    VALIDATED = "validated"  # Preconditions checked
    COMMITTED = "committed"  # Successfully applied
    FAILED = "failed"  # Failed validation or commit
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
    group_id: str = Field(
        default="",
        description="Effects sharing a group_id commit or fail as one "
        "(when atomicity='group'); '' = singleton group",
    )
    atomicity: str = Field(
        default="per_effect",
        description="'per_effect' = independent failure; 'group' = any "
        "member failure fails the whole group (no tick "
        "rollback — local effect-level failure)",
    )
    invert_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Prior-value data captured at apply time (SPEC §3.3 "
        "回滚=逆操作): the minimal state this effect needs to "
        "undo itself. Written during apply, read by the "
        "single rollback entry.",
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
        group_id: str = "",
        atomicity: str = "per_effect",
    ) -> StagedEffect:
        """Stage a new effect for later commit.

        group_id + atomicity="group": members commit or fail as one
        (used by multi-effect intents like delegate → TASK_CREATE +
        EMAIL_SEND).
        """
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
            group_id=group_id,
            atomicity=atomicity,
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
        check_task: Callable[[StagedEffect], str | None] | None = None,
    ) -> list[StagedEffect]:
        """Validate all staged effects against preconditions.

        check_lock signature: (resource, agent_id, lock_token) -> bool
        check_task signature: (effect) -> error message or None; applies
        to TASK_UPDATE effects (task still exists / not cancelled /
        not already terminal). CommitValidate: "is it still committable
        now?"

        Note: apply-time checks (e.g. FILE_PATCH base-hash re-check,
        which must see same-tick writes already applied) live in the
        caller's apply loop, not here.

        Returns list of effects that failed validation.
        """
        failures: list[StagedEffect] = []

        for effect in self._effects.values():
            if effect.status != EffectStatus.STAGED:
                continue

            # Task check (TASK_UPDATE effects must target a live task)
            if effect.effect_type == EffectType.TASK_UPDATE and check_task:
                error = check_task(effect)
                if error is not None:
                    effect.status = EffectStatus.FAILED
                    effect.error = error
                    failures.append(effect)
                    continue

            # Version check
            if effect.expected_version is not None and check_version:
                if not check_version(effect.resource, effect.expected_version):
                    effect.status = EffectStatus.FAILED
                    effect.error = f"Version conflict: expected {effect.expected_version}"
                    failures.append(effect)
                    continue

            # Lock check (for write operations) — verifies both ownership
            # and lock_token of the staged effect
            if (
                effect.effect_type
                in {
                    EffectType.KB_WRITE,
                    EffectType.KB_CREATE,
                    EffectType.KB_DELETE,
                    EffectType.FILE_WRITE,
                    EffectType.FILE_PATCH,
                    EffectType.FILE_DELETE,
                }
                and check_lock
            ):
                if not check_lock(effect.resource, effect.agent_id, effect.lock_token):
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

        # Group atomicity: if any member of a 'group'-atomicity group
        # failed, ALL members fail together (e.g. a DelegateIntent's
        # TASK_CREATE + EMAIL_SEND must commit or fail as one).
        groups: dict[str, list[StagedEffect]] = {}
        for effect in self._effects.values():
            if effect.group_id and effect.atomicity == "group":
                groups.setdefault(effect.group_id, []).append(effect)
        for group_id, members in groups.items():
            if any(m.status == EffectStatus.FAILED for m in members):
                for member in members:
                    if member.status != EffectStatus.FAILED:
                        member.status = EffectStatus.FAILED
                        member.error = f"group member failed (group {group_id})"
                        failures.append(member)

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
            winner_effects = sorted(by_agent[winner_agent], key=lambda e: e.effect_id)

            loser_effects = []
            for agent_id in sorted_agents[1:]:
                loser_effects.extend(sorted(by_agent[agent_id], key=lambda e: e.effect_id))

            for loser in loser_effects:
                loser.status = EffectStatus.FAILED
                loser.error = f"Conflict: lost to agent {winner_agent}"
                # Group atomicity: a member that lost a conflict fails
                # the whole group with it.
                if loser.group_id and loser.atomicity == "group":
                    for effect in self._effects.values():
                        if (
                            effect.group_id == loser.group_id
                            and effect.status != EffectStatus.FAILED
                        ):
                            effect.status = EffectStatus.FAILED
                            effect.error = f"group member lost conflict (group {loser.group_id})"

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
        return sum(1 for e in self._effects.values() if e.status == EffectStatus.FAILED)

    def summary(self) -> dict[str, Any]:
        """Get a summary of the transaction state."""
        staged = sum(1 for e in self._effects.values() if e.status == EffectStatus.STAGED)
        validated = sum(1 for e in self._effects.values() if e.status == EffectStatus.VALIDATED)
        return {
            "total_effects": len(self._effects),
            "staged": staged,
            "validated": validated,
            "committed": self.committed_count,
            "failed": self.failed_count,
            "conflicts": len(self._conflict_resolutions),
        }
