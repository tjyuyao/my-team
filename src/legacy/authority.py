"""Authority resolution core — the decision-adjudication primitive.

Standalone and dependency-free: this module implements ONLY the
Authority model (8 domains, the AuthorityGrant 7-tuple) and the
resolution algorithm (context matching, composition evaluation, veto
handling, unresolved semantics, escalation triggering). It deliberately
does not import the kernel (no tick, no journal, no simulation): wiring
into the Validate/Commit phases is a later integration task.

Authority answers "when several subjects make legitimate claims on one
effect, whose claim becomes binding". It is NOT permission (deny-by-
default authorization lives in the closure), NOT accountability, NOT
assignment.

Design references:
- KANBAN/OPEN_ISSUE/2026-08-18-extension-surface-spec.md (Authority
  model, governance graph, 4 governance invariants)
- KANBAN/TODO/2026-08-18-authority-evaluation.md

Semantics locked in v1:
- resolution is DOMAIN-scoped: all grants on the domain participate,
  subject is not a filter — a veto or a competing final binds the claim
  regardless of who asks; the requester's subject only decides whether
  THEY may be the decider (ALLOWED) or must await the holder's consent
  (WAITING);
- `unresolved` is never silently resolved (no random / last-writer /
  higher-rank / first-writer winner);
- a condition evaluating UNKNOWN is never treated as passed;
- context specificity: exact > "prefix:*" wildcard > unscoped; an
  unresolved conflict at a more specific tier does NOT fall through to
  a less specific tier;
- an effective veto blocks regardless of finals;
- escalation does not transfer ownership (governance invariant 3 — the
  result only names the target, it never rewrites grants).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Domain(str, Enum):
    """The 8 generic decision domains (data-layer labels, not business words)."""

    SCOPE = "scope"
    CONTENT = "content"
    METHOD = "method"
    SCHEDULE = "schedule"
    COST = "cost"
    ACCEPTANCE = "acceptance"
    RELEASE = "release"
    OWNERSHIP = "ownership"


class Strength(str, Enum):
    """Grant strength. `none` is the implicit default (declared or not)."""

    FINAL = "final"
    VETO = "veto"
    CONSULT = "consult"
    NONE = "none"


class Composition(str, Enum):
    """Synthesis rule for MULTIPLE finals — not a fourth strength tier."""

    PRIORITY = "priority"
    JOINT = "joint"
    THRESHOLD = "threshold"


class EscalationMode(str, Enum):
    """What escalation does with the matter (does NOT change ownership)."""

    ARBITRATE = "arbitrate"
    TRANSFER = "transfer"
    ADVISE = "advise"


class EscalationTrigger(str, Enum):
    """What condition routes the matter upward."""

    UNRESOLVED = "unresolved"
    CONDITION_BREACHED = "condition_breached"
    EXCEPTION = "exception"


class DecisionState(str, Enum):
    ALLOWED = "allowed"        # a decider exists and decided
    BLOCKED = "blocked"        # an effective veto fired
    UNRESOLVED = "unresolved"  # no decider, or explicit conflict w/o composition
    WAITING = "waiting"        # joint/threshold consent not yet complete


class ConditionOp(str, Enum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EXISTS = "exists"


class CondTruth(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Condition:
    """A declarative condition over a context dict (L0 mini-DSL).

    Evaluation is three-valued: a missing field or a type mismatch
    yields UNKNOWN — which is NEVER treated as passed.
    """

    field: str
    op: ConditionOp
    value: Any = None

    def __post_init__(self) -> None:
        if isinstance(self.op, str):
            object.__setattr__(self, "op", ConditionOp(self.op))

    def evaluate(self, ctx: dict[str, Any]) -> CondTruth:
        present = self.field in ctx
        if self.op is ConditionOp.EXISTS:
            want = bool(self.value) if self.value is not None else True
            return CondTruth.TRUE if present == want else CondTruth.FALSE
        if not present:
            return CondTruth.UNKNOWN
        v = ctx[self.field]
        if self.op is ConditionOp.EQ:
            return CondTruth.TRUE if v == self.value else CondTruth.FALSE
        if self.op is ConditionOp.NE:
            return CondTruth.TRUE if v != self.value else CondTruth.FALSE
        if self.op in (ConditionOp.IN, ConditionOp.NOT_IN):
            if not isinstance(self.value, (list, set, tuple, frozenset)):
                return CondTruth.UNKNOWN
            inside = v in self.value
            wanted = self.op is ConditionOp.IN
            return CondTruth.TRUE if inside == wanted else CondTruth.FALSE
        if self.op in (ConditionOp.GT, ConditionOp.GTE, ConditionOp.LT, ConditionOp.LTE):
            try:
                if self.op is ConditionOp.GT:
                    ok = v > self.value
                elif self.op is ConditionOp.GTE:
                    ok = v >= self.value
                elif self.op is ConditionOp.LT:
                    ok = v < self.value
                else:
                    ok = v <= self.value
            except TypeError:
                return CondTruth.UNKNOWN
            return CondTruth.TRUE if ok else CondTruth.FALSE
        return CondTruth.UNKNOWN


@dataclass(frozen=True)
class Escalation:
    on: EscalationTrigger
    mode: EscalationMode
    target: str


@dataclass(frozen=True)
class AuthorityGrant:
    """The AuthorityGrant 7-tuple: subject × domain × context × strength
    × composition × conditions × escalation. Two extra numeric fields
    exist only to parameterize compositions (priority / threshold).

    Accepts YAML/JSON-style strings for enums (coerced in __post_init__).
    """

    subject: str
    domain: Domain
    context: str | None = None        # data-layer label; None = unscoped
    strength: Strength = Strength.NONE
    composition: Composition | None = None
    conditions: tuple[Condition, ...] = ()
    escalation: Escalation | None = None
    priority: int = 0                 # Composition.PRIORITY: higher wins
    threshold_n: int = 1              # Composition.THRESHOLD: N of M

    def __post_init__(self) -> None:
        if isinstance(self.domain, str):
            object.__setattr__(self, "domain", Domain(self.domain))
        if isinstance(self.strength, str):
            object.__setattr__(self, "strength", Strength(self.strength))
        if isinstance(self.composition, str):
            object.__setattr__(self, "composition", Composition(self.composition))
        if isinstance(self.escalation, dict):
            object.__setattr__(self, "escalation", Escalation(**self.escalation))
        conds = tuple(
            Condition(**c) if isinstance(c, dict) else c for c in self.conditions
        )
        object.__setattr__(self, "conditions", conds)


class DecisionRequest(BaseModel):
    """A single resolution request for ONE domain.

    `subject` is the resolved principal/role making the claim (e.g.
    "@owner" when the Owner acts). `consenting` lists subjects that have
    already agreed (joint/threshold evaluation) — the requester's own
    consent is implicit. `exception` lets the caller flag an exceptional
    situation to route via escalation on=exception.
    """

    subject: str
    domain: Domain
    context: str | None = None
    condition_context: dict[str, Any] = Field(default_factory=dict)
    consenting: frozenset[str] = Field(default_factory=frozenset)
    exception: bool = False


class DecisionResult(BaseModel):
    state: DecisionState
    domain: Domain
    winner: str | None = None
    reason: str = ""
    matched: list[str] = Field(default_factory=list)
    effective_finals: list[str] = Field(default_factory=list)
    effective_vetoes: list[str] = Field(default_factory=list)
    consults: list[str] = Field(default_factory=list)
    escalation: Escalation | None = None


class DecisionClaim(BaseModel):
    """Kernel-facing claim wrapper (a claim may span multiple domains).

    An effect is admissible only if every domain resolves to ALLOWED
    (see claim_overall). `authority_snapshot` pins the grant set version
    for replay/audit (filled by the integration layer).
    """

    claim_id: str
    subject: str
    effect_ref: str
    domains: list[Domain]
    context: str | None = None
    condition_context: dict[str, Any] = Field(default_factory=dict)
    consenting: frozenset[str] = Field(default_factory=frozenset)
    process_instance_id: str = ""
    authority_snapshot: str = ""


def _desc(g: AuthorityGrant) -> str:
    return f"{g.subject}@{g.domain.value}:{g.context or '*'}"


def _context_specificity(grant_ctx: str | None, request_ctx: str | None) -> int:
    """Context match specificity: 3=exact, 2=wildcard, 1=unscoped, 0=no match."""
    if grant_ctx is None:
        return 1
    if request_ctx is None:
        return 0
    if grant_ctx == request_ctx:
        return 3
    if grant_ctx.endswith("*") and request_ctx.startswith(grant_ctx[:-1]):
        return 2
    return 0


def _grant_effective(g: AuthorityGrant, ctx: dict[str, Any]) -> bool:
    """Effective iff every condition is TRUE (UNKNOWN never counts)."""
    return all(c.evaluate(ctx) is CondTruth.TRUE for c in g.conditions)


def _escalation_for(
    grants: Sequence[AuthorityGrant],
    trigger: EscalationTrigger,
    request: DecisionRequest,
) -> Escalation | None:
    if request.exception:
        trigger = EscalationTrigger.EXCEPTION
    for g in grants:
        if g.escalation is not None and g.escalation.on == trigger:
            return g.escalation
    return None


def resolve(request: DecisionRequest, grants: Sequence[AuthorityGrant]) -> DecisionResult:
    """Resolve one decision claim (single domain) against a grant set.

    v1 algorithm (locked order):
    1. consider ALL grants on the domain — subject is not a filter, a
       veto or a competing final binds the claim regardless of who asks;
    2. tier by context specificity, most specific first;
    3. within a tier, keep only EFFECTIVE grants (conditions all TRUE);
    4. any effective veto → BLOCKED;
    5. 0 finals → fall through to a less specific tier;
    6. single binding final: requester == holder → ALLOWED, otherwise
       WAITING (the holder's consent is required — maps to the human
       approval flow);
    7. multiple finals: require ONE composition and evaluate it
       (priority → top holder decides; joint/threshold → holders'
       consent); missing or ambiguous composition → UNRESOLVED (never
       pick a winner);
    8. no decider anywhere → UNRESOLVED.

    A child acting under delegation inherits its delegator's grants at
    integration time (governance invariant 4); this core only evaluates
    the grant set it is given.
    """
    domain_grants = [g for g in grants if g.domain == request.domain]
    if not domain_grants:
        return DecisionResult(
            state=DecisionState.UNRESOLVED,
            domain=request.domain,
            reason="no authority grant for domain",
            escalation=_escalation_for(
                domain_grants, EscalationTrigger.UNRESOLVED, request
            ),
        )

    matched = [_desc(g) for g in domain_grants]
    tiers: dict[int, list[AuthorityGrant]] = {}
    for g in domain_grants:
        spec = _context_specificity(g.context, request.context)
        if spec > 0:
            tiers.setdefault(spec, []).append(g)

    seen_consults: list[str] = []
    # The requester consents to their own claim implicitly.
    consent = set(request.consenting) | {request.subject}

    for spec in sorted(tiers, reverse=True):
        tier = tiers[spec]
        effective = [g for g in tier if _grant_effective(g, request.condition_context)]

        vetoes = [g for g in effective if g.strength is Strength.VETO]
        if vetoes:
            return DecisionResult(
                state=DecisionState.BLOCKED,
                domain=request.domain,
                reason=f"effective veto by {vetoes[0].subject}",
                matched=matched,
                effective_vetoes=[_desc(g) for g in vetoes],
                consults=seen_consults,
                escalation=_escalation_for(
                    vetoes, EscalationTrigger.CONDITION_BREACHED, request
                ),
            )

        finals = [g for g in effective if g.strength is Strength.FINAL]
        seen_consults.extend(
            _desc(g) for g in effective if g.strength is Strength.CONSULT
        )
        if not finals:
            continue  # nothing decides at this tier

        if len(finals) == 1:
            holder = finals[0]
        else:
            comps = {f.composition for f in finals}
            if None in comps or len(comps) > 1:
                return DecisionResult(
                    state=DecisionState.UNRESOLVED,
                    domain=request.domain,
                    reason="multiple finals without a single composition",
                    matched=matched,
                    effective_finals=[_desc(f) for f in finals],
                    consults=seen_consults,
                    escalation=_escalation_for(
                        domain_grants, EscalationTrigger.UNRESOLVED, request
                    ),
                )
            comp = finals[0].composition
            assert comp is not None  # checked above
            if comp is Composition.PRIORITY:
                best_prio = max(f.priority for f in finals)
                best = [f for f in finals if f.priority == best_prio]
                if len(best) > 1:
                    return DecisionResult(
                        state=DecisionState.UNRESOLVED,
                        domain=request.domain,
                        reason="priority composition: tie — no implicit winner",
                        matched=matched,
                        effective_finals=[_desc(f) for f in finals],
                        consults=seen_consults,
                        escalation=_escalation_for(
                            domain_grants, EscalationTrigger.UNRESOLVED, request
                        ),
                    )
                holder = best[0]
            elif comp is Composition.JOINT:
                needed = {f.subject for f in finals}
                missing = sorted(needed - consent)
                if missing:
                    return DecisionResult(
                        state=DecisionState.WAITING,
                        domain=request.domain,
                        reason=f"joint consent missing: {missing}",
                        matched=matched,
                        effective_finals=[_desc(f) for f in finals],
                        consults=seen_consults,
                    )
                return DecisionResult(
                    state=DecisionState.ALLOWED,
                    domain=request.domain,
                    winner="joint:" + ",".join(sorted(needed)),
                    reason="joint consent complete",
                    matched=matched,
                    effective_finals=[_desc(f) for f in finals],
                    consults=seen_consults,
                )
            else:  # Composition.THRESHOLD
                n = max(1, finals[0].threshold_n)
                consented = sorted(consent & {f.subject for f in finals})
                if len(consented) < n:
                    return DecisionResult(
                        state=DecisionState.WAITING,
                        domain=request.domain,
                        reason=f"threshold {len(consented)}/{n} not met",
                        matched=matched,
                        effective_finals=[_desc(f) for f in finals],
                        consults=seen_consults,
                    )
                return DecisionResult(
                    state=DecisionState.ALLOWED,
                    domain=request.domain,
                    winner="threshold:" + ",".join(consented),
                    reason=f"threshold {len(consented)}/{n} met",
                    matched=matched,
                    effective_finals=[_desc(f) for f in finals],
                    consults=seen_consults,
                )

        if request.subject == holder.subject:
            return DecisionResult(
                state=DecisionState.ALLOWED,
                domain=request.domain,
                winner=holder.subject,
                reason=f"{holder.subject} decides",
                matched=matched,
                effective_finals=[_desc(f) for f in finals],
                consults=seen_consults,
            )
        return DecisionResult(
            state=DecisionState.WAITING,
            domain=request.domain,
            reason=f"decision belongs to {holder.subject} (consent required)",
            matched=matched,
            effective_finals=[_desc(f) for f in finals],
            consults=seen_consults,
        )

    return DecisionResult(
        state=DecisionState.UNRESOLVED,
        domain=request.domain,
        reason="no final authority for domain/context",
        matched=matched,
        consults=seen_consults,
        escalation=_escalation_for(
            domain_grants, EscalationTrigger.UNRESOLVED, request
        ),
    )


def resolve_claim(
    claim: DecisionClaim, grants: Sequence[AuthorityGrant]
) -> dict[Domain, DecisionResult]:
    """Resolve every domain of a claim (one result per domain)."""
    out: dict[Domain, DecisionResult] = {}
    for d in claim.domains:
        req = DecisionRequest(
            subject=claim.subject,
            domain=d,
            context=claim.context,
            condition_context=claim.condition_context,
            consenting=claim.consenting,
        )
        out[d] = resolve(req, grants)
    return out


def claim_overall(results: dict[Domain, DecisionResult]) -> DecisionState:
    """Aggregate per-domain results for one claim.

    An effect must satisfy ALL its domains. Precedence when aggregating:
    BLOCKED > UNRESOLVED > WAITING > ALLOWED.
    """
    states = [r.state for r in results.values()]
    if any(s is DecisionState.BLOCKED for s in states):
        return DecisionState.BLOCKED
    if any(s is DecisionState.UNRESOLVED for s in states):
        return DecisionState.UNRESOLVED
    if any(s is DecisionState.WAITING for s in states):
        return DecisionState.WAITING
    return DecisionState.ALLOWED


def _context_covers(delegator_ctx: str | None, child_ctx: str | None) -> bool:
    """Whether a delegator grant's context covers a child grant's context."""
    if delegator_ctx is None:
        return True
    if child_ctx is None:
        return False  # delegator scoped, child unscoped → child is broader
    if delegator_ctx == child_ctx:
        return True
    if delegator_ctx.endswith("*") and child_ctx.startswith(delegator_ctx[:-1]):
        return True
    if child_ctx.endswith("*") and delegator_ctx.startswith(child_ctx[:-1]):
        return True
    return False


def check_delegation_monotonic(
    delegator: Sequence[AuthorityGrant], child: Sequence[AuthorityGrant]
) -> list[str]:
    """Static slice of governance invariant 4 (delegation monotonicity).

    Every child grant with deciding strength (FINAL/VETO) must be
    covered by a delegator grant on the same domain whose context is at
    least as broad. Returns violation descriptions (empty = OK).

    NOTE: this is the static half. The kernel must still re-verify the
    invariant dynamically at every delegate action.
    """
    violations: list[str] = []
    for cg in child:
        if cg.strength not in (Strength.FINAL, Strength.VETO):
            continue
        covered = any(
            dg.domain == cg.domain
            and dg.strength in (Strength.FINAL, Strength.VETO)
            and _context_covers(dg.context, cg.context)
            for dg in delegator
        )
        if not covered:
            violations.append(
                f"child grant {_desc(cg)} not covered by delegator authority"
            )
    return violations
