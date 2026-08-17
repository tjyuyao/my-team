"""Tests for the Authority resolution core (standalone module).

Covers KANBAN/TODO/2026-08-18-authority-evaluation.md acceptance:
- multiple finals without composition → unresolved (never implicit winner)
- any effective veto → blocked (regardless of requester)
- deterministic context matching (specificity high-to-low)
- condition UNKNOWN is not treated as passed
- requester holds the final → allowed; otherwise waiting (approval flow)
- composition evaluation (priority / joint / threshold)
- escalation triggering (unresolved / condition_breached / exception)
- delegation monotonicity static check
"""

from __future__ import annotations

from my_team.authority import (
    AuthorityGrant,
    Condition,
    ConditionOp,
    DecisionClaim,
    DecisionRequest,
    DecisionResult,
    DecisionState,
    Domain,
    Escalation,
    EscalationMode,
    EscalationTrigger,
    Strength,
    check_delegation_monotonic,
    claim_overall,
    resolve,
    resolve_claim,
)

GR = AuthorityGrant
FIN = Strength.FINAL
VETO = Strength.VETO
CONS = Strength.CONSULT


def req(subject, domain, context=None, **kw) -> DecisionRequest:
    return DecisionRequest(subject=subject, domain=domain, context=context, **kw)


# --- single final -----------------------------------------------------------


def test_holder_claim_allowed():
    grants = [GR(subject="editor", domain=Domain.CONTENT, strength=FIN)]
    r = resolve(req("editor", Domain.CONTENT), grants)
    assert r.state is DecisionState.ALLOWED
    assert r.winner == "editor"


def test_non_holder_claim_waits_for_decider():
    grants = [GR(subject="editor", domain=Domain.CONTENT, strength=FIN)]
    r = resolve(req("reviewer", Domain.CONTENT), grants)
    assert r.state is DecisionState.WAITING  # editor must consent
    assert r.winner is None


def test_no_grant_is_unresolved_not_allowed():
    r = resolve(req("editor", Domain.CONTENT), [])
    assert r.state is DecisionState.UNRESOLVED
    assert r.winner is None


# --- veto -------------------------------------------------------------------


def test_effective_veto_blocks_final():
    grants = [
        GR(subject="editor", domain=Domain.RELEASE, strength=FIN),
        GR(subject="compliance", domain=Domain.RELEASE, strength=VETO),
    ]
    r = resolve(req("editor", Domain.RELEASE), grants)
    assert r.state is DecisionState.BLOCKED
    assert r.effective_vetoes == ["compliance@release:*"]


def test_conditional_veto_fires_only_when_condition_true():
    grants = [
        GR(subject="editor", domain=Domain.RELEASE, strength=FIN),
        GR(
            subject="compliance",
            domain=Domain.RELEASE,
            strength=VETO,
            conditions=(Condition(field="risk", op=ConditionOp.GT, value=3),),
        ),
    ]
    high = resolve(req("editor", Domain.RELEASE, condition_context={"risk": 5}), grants)
    assert high.state is DecisionState.BLOCKED
    low = resolve(req("editor", Domain.RELEASE, condition_context={"risk": 1}), grants)
    assert low.state is DecisionState.ALLOWED


# --- unresolved semantics ---------------------------------------------------


def test_two_finals_without_composition_is_unresolved():
    grants = [
        GR(subject="editor", domain=Domain.CONTENT, strength=FIN),
        GR(subject="chief", domain=Domain.CONTENT, strength=FIN),
    ]
    r = resolve(req("editor", Domain.CONTENT), grants)
    assert r.state is DecisionState.UNRESOLVED
    assert r.winner is None  # never an implicit winner


def test_priority_composition_top_holder_decides():
    grants = [
        GR(subject="editor", domain=Domain.CONTENT, strength=FIN,
            composition="priority", priority=1),
        GR(subject="chief", domain=Domain.CONTENT, strength=FIN,
            composition="priority", priority=5),
    ]
    as_chief = resolve(req("chief", Domain.CONTENT), grants)
    assert as_chief.state is DecisionState.ALLOWED
    assert as_chief.winner == "chief"
    as_editor = resolve(req("editor", Domain.CONTENT), grants)
    assert as_editor.state is DecisionState.WAITING  # chief must decide


def test_priority_tie_is_unresolved():
    grants = [
        GR(subject="editor", domain=Domain.CONTENT, strength=FIN,
            composition="priority", priority=1),
        GR(subject="chief", domain=Domain.CONTENT, strength=FIN,
            composition="priority", priority=1),
    ]
    assert resolve(req("editor", Domain.CONTENT), grants).state is DecisionState.UNRESOLVED


def test_mixed_compositions_is_unresolved():
    grants = [
        GR(subject="a", domain=Domain.SCOPE, strength=FIN, composition="priority"),
        GR(subject="b", domain=Domain.SCOPE, strength=FIN, composition="joint"),
    ]
    assert resolve(req("a", Domain.SCOPE), grants).state is DecisionState.UNRESOLVED


# --- joint / threshold ------------------------------------------------------


def test_joint_consent_complete_allowed():
    grants = [
        GR(subject="a", domain=Domain.SCOPE, strength=FIN, composition="joint"),
        GR(subject="b", domain=Domain.SCOPE, strength=FIN, composition="joint"),
    ]
    r = resolve(req("a", Domain.SCOPE, consenting=frozenset({"b"})), grants)
    assert r.state is DecisionState.ALLOWED  # requester a consents implicitly
    assert r.winner == "joint:a,b"


def test_joint_missing_consent_waits():
    grants = [
        GR(subject="a", domain=Domain.SCOPE, strength=FIN, composition="joint"),
        GR(subject="b", domain=Domain.SCOPE, strength=FIN, composition="joint"),
    ]
    r = resolve(req("a", Domain.SCOPE, consenting=frozenset()), grants)
    assert r.state is DecisionState.WAITING


def test_threshold_2_of_3():
    grants = [
        GR(subject="a", domain=Domain.ACCEPTANCE, strength=FIN,
            composition="threshold", threshold_n=2),
        GR(subject="b", domain=Domain.ACCEPTANCE, strength=FIN,
            composition="threshold", threshold_n=2),
        GR(subject="c", domain=Domain.ACCEPTANCE, strength=FIN,
            composition="threshold", threshold_n=2),
    ]
    ok = resolve(req("a", Domain.ACCEPTANCE, consenting=frozenset({"c"})), grants)
    assert ok.state is DecisionState.ALLOWED  # a (implicit) + c = 2
    pending = resolve(req("a", Domain.ACCEPTANCE, consenting=frozenset()), grants)
    assert pending.state is DecisionState.WAITING


# --- conditions -------------------------------------------------------------


def test_condition_false_disables_final_leading_to_unresolved():
    grants = [
        GR(
            subject="editor",
            domain=Domain.CONTENT,
            strength=FIN,
            conditions=(Condition(field="genre", op=ConditionOp.EQ, value="essay"),),
        ),
    ]
    r = resolve(req("editor", Domain.CONTENT, condition_context={"genre": "novel"}), grants)
    assert r.state is DecisionState.UNRESOLVED


def test_condition_unknown_is_not_passed():
    grants = [
        GR(
            subject="editor",
            domain=Domain.CONTENT,
            strength=FIN,
            conditions=(Condition(field="genre", op=ConditionOp.EQ, value="essay"),),
        ),
    ]
    # genre missing from context → UNKNOWN → final not effective → unresolved
    r = resolve(req("editor", Domain.CONTENT, condition_context={}), grants)
    assert r.state is DecisionState.UNRESOLVED


def test_veto_with_unknown_condition_does_not_fire():
    grants = [
        GR(subject="editor", domain=Domain.RELEASE, strength=FIN),
        GR(
            subject="compliance",
            domain=Domain.RELEASE,
            strength=VETO,
            conditions=(Condition(field="risk", op=ConditionOp.GT, value=3),),
        ),
    ]
    r = resolve(req("editor", Domain.RELEASE, condition_context={}), grants)
    assert r.state is DecisionState.ALLOWED


def test_condition_ops():
    ctx = {"count": 2, "name": "x", "tags": ["a"]}
    assert Condition(field="count", op=ConditionOp.GTE, value=2).evaluate(ctx) == "true"
    assert Condition(field="count", op=ConditionOp.LT, value=2).evaluate(ctx) == "false"
    assert Condition(field="name", op=ConditionOp.IN, value=["x", "y"]).evaluate(ctx) == "true"
    assert Condition(field="name", op=ConditionOp.NOT_IN, value=["z"]).evaluate(ctx) == "true"
    assert Condition(field="missing", op=ConditionOp.EXISTS).evaluate(ctx) == "false"
    assert Condition(field="count", op=ConditionOp.GT, value="x").evaluate(ctx) == "unknown"
    assert Condition(field="absent", op=ConditionOp.EQ, value=1).evaluate(ctx) == "unknown"


# --- context specificity ----------------------------------------------------


def test_exact_context_beats_unscoped():
    grants = [
        GR(subject="owner", domain=Domain.CONTENT, strength=FIN),
        GR(subject="editor", domain=Domain.CONTENT, context="essay", strength=FIN),
    ]
    r = resolve(req("editor", Domain.CONTENT, context="essay"), grants)
    assert r.state is DecisionState.ALLOWED
    assert r.winner == "editor"


def test_wildcard_between_exact_and_unscoped():
    grants = [
        GR(subject="owner", domain=Domain.CONTENT, strength=FIN),
        GR(subject="editor", domain=Domain.CONTENT, context="essay:*", strength=FIN),
    ]
    r = resolve(req("editor", Domain.CONTENT, context="essay:chapter2"), grants)
    assert r.state is DecisionState.ALLOWED
    assert r.winner == "editor"


def test_specific_unresolved_does_not_fall_through_to_generic():
    grants = [
        GR(subject="owner", domain=Domain.CONTENT, strength=FIN),
        GR(subject="e1", domain=Domain.CONTENT, context="essay", strength=FIN),
        GR(subject="e2", domain=Domain.CONTENT, context="essay", strength=FIN),
    ]
    r = resolve(req("e1", Domain.CONTENT, context="essay"), grants)
    assert r.state is DecisionState.UNRESOLVED  # must escalate, not fall through


def test_specific_effective_veto_over_generic_final():
    grants = [
        GR(subject="owner", domain=Domain.CONTENT, strength=FIN),
        GR(subject="guard", domain=Domain.CONTENT, context="essay", strength=VETO),
    ]
    r = resolve(req("guard", Domain.CONTENT, context="essay"), grants)
    assert r.state is DecisionState.BLOCKED


# --- escalation -------------------------------------------------------------


def test_unresolved_escalation_target():
    grants = [
        GR(
            subject="e1",
            domain=Domain.CONTENT,
            strength=FIN,
            escalation=Escalation(
                on=EscalationTrigger.UNRESOLVED, mode=EscalationMode.ARBITRATE, target="@owner"
            ),
        ),
        GR(subject="e2", domain=Domain.CONTENT, strength=FIN),
    ]
    r = resolve(req("e1", Domain.CONTENT), grants)
    assert r.state is DecisionState.UNRESOLVED
    assert r.escalation is not None
    assert r.escalation.target == "@owner"
    assert r.escalation.mode is EscalationMode.ARBITRATE


def test_blocked_escalation_on_condition_breached():
    grants = [
        GR(
            subject="compliance",
            domain=Domain.RELEASE,
            strength=VETO,
            escalation=Escalation(
                on=EscalationTrigger.CONDITION_BREACHED, mode=EscalationMode.ADVISE, target="@owner"
            ),
        ),
    ]
    r = resolve(req("compliance", Domain.RELEASE), grants)
    assert r.state is DecisionState.BLOCKED
    assert r.escalation is not None and r.escalation.target == "@owner"


def test_exception_flag_routes_to_exception_escalation():
    grants = [
        GR(
            subject="e1",
            domain=Domain.SCOPE,
            strength=FIN,
            escalation=Escalation(
                on=EscalationTrigger.EXCEPTION, mode=EscalationMode.TRANSFER, target="@owner"
            ),
        ),
        GR(subject="e2", domain=Domain.SCOPE, strength=FIN),
    ]
    r = resolve(req("e1", Domain.SCOPE, exception=True), grants)
    assert r.state is DecisionState.UNRESOLVED
    assert r.escalation is not None and r.escalation.on is EscalationTrigger.EXCEPTION


# --- consult ----------------------------------------------------------------


def test_consult_is_recorded_but_does_not_decide():
    grants = [
        GR(subject="editor", domain=Domain.CONTENT, strength=FIN),
        GR(subject="expert", domain=Domain.CONTENT, strength=CONS),
    ]
    r = resolve(req("editor", Domain.CONTENT), grants)
    assert r.state is DecisionState.ALLOWED
    assert r.consults == ["expert@content:*"]


def test_only_consults_is_unresolved():
    grants = [GR(subject="expert", domain=Domain.CONTENT, strength=CONS)]
    r = resolve(req("expert", Domain.CONTENT), grants)
    assert r.state is DecisionState.UNRESOLVED


# --- delegation monotonicity (invariant 4, static half) ---------------------


def test_delegation_monotonic_ok():
    delegator = [GR(subject="manager", domain=Domain.CONTENT, context="essay:*", strength=FIN)]
    child = [
        GR(subject="sub", domain=Domain.CONTENT, context="essay:chapter2", strength=FIN),
        GR(subject="sub", domain=Domain.CONTENT, context="essay:chapter1", strength=VETO),
    ]
    assert check_delegation_monotonic(delegator, child) == []


def test_delegation_monotonic_violations():
    delegator = [GR(subject="manager", domain=Domain.CONTENT, context="essay", strength=FIN)]
    child = [
        GR(subject="sub", domain=Domain.CONTENT, context="essay:*", strength=FIN),
        GR(subject="sub", domain=Domain.COST, strength=FIN),
    ]
    violations = check_delegation_monotonic(delegator, child)
    assert len(violations) == 2


def test_consult_only_delegator_cannot_delegate_deciding_power():
    delegator = [GR(subject="manager", domain=Domain.CONTENT, strength=CONS)]
    child = [GR(subject="sub", domain=Domain.CONTENT, strength=FIN)]
    assert len(check_delegation_monotonic(delegator, child)) == 1


# --- claim multi-domain -----------------------------------------------------


def test_resolve_claim_multiple_domains_and_overall():
    grants = [
        GR(subject="editor", domain=Domain.CONTENT, strength=FIN),
        GR(subject="editor", domain=Domain.SCHEDULE, strength=FIN),
        GR(subject="compliance", domain=Domain.RELEASE, strength=VETO),
    ]
    claim = DecisionClaim(
        claim_id="c1",
        subject="editor",
        effect_ref="eff.1",
        domains=[Domain.CONTENT, Domain.SCHEDULE, Domain.RELEASE],
    )
    results = resolve_claim(claim, grants)
    assert results[Domain.CONTENT].state is DecisionState.ALLOWED
    assert results[Domain.SCHEDULE].state is DecisionState.ALLOWED
    assert results[Domain.RELEASE].state is DecisionState.BLOCKED
    assert claim_overall(results) is DecisionState.BLOCKED


def test_claim_overall_precedence():
    def res(state: DecisionState) -> DecisionResult:
        return DecisionResult(state=state, domain=Domain.SCOPE)

    mixed = {"a": res(DecisionState.ALLOWED), "b": res(DecisionState.WAITING)}
    assert claim_overall(mixed) is DecisionState.WAITING
    mixed2 = {"a": res(DecisionState.WAITING), "b": res(DecisionState.UNRESOLVED)}
    assert claim_overall(mixed2) is DecisionState.UNRESOLVED
    mixed3 = {"a": res(DecisionState.UNRESOLVED), "b": res(DecisionState.BLOCKED)}
    assert claim_overall(mixed3) is DecisionState.BLOCKED


# --- determinism ------------------------------------------------------------


def test_resolution_is_deterministic():
    grants = [
        GR(subject="e1", domain=Domain.CONTENT, strength=FIN),
        GR(subject="e2", domain=Domain.CONTENT, strength=FIN, composition="priority", priority=2),
        GR(subject="guard", domain=Domain.CONTENT, context="essay", strength=VETO),
    ]
    r1 = resolve(req("e1", Domain.CONTENT, context="essay"), grants)
    r2 = resolve(req("e1", Domain.CONTENT, context="essay"), grants)
    assert r1.model_dump() == r2.model_dump()
    assert r1.state is DecisionState.BLOCKED


def test_grant_accepts_json_strings():
    grants = [
        GR(subject="editor", domain="content", strength="final"),
        GR(
            subject="guard",
            domain="content",
            context="essay",
            strength="veto",
            conditions=[{"field": "risk", "op": "gt", "value": 3}],
            escalation={"on": "condition_breached", "mode": "advise", "target": "@owner"},
        ),
    ]
    r = resolve(
        req("editor", Domain.CONTENT, context="essay", condition_context={"risk": 5}),
        grants,
    )
    assert r.state is DecisionState.BLOCKED
    assert r.escalation is not None and r.escalation.target == "@owner"
