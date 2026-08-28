"""SPEC §16 "ActionGate" + "Lease": the single choke point, approval tokens, transition table."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from anton.policy.gate import (
    LEASE_TRANSITIONS,
    ActionGate,
    Allow,
    ApprovalStore,
    ApprovalToken,
    Deny,
    DenyReason,
    GateContext,
    LeaseState,
    NeedsApproval,
    PolicyConfig,
    can_transition,
    resolve_secret,
)
from anton.schema.artifact import ActionType, RiskClass
from anton.schema.snapshot import BBox
from anton.surface.base import ConcreteAction, ResolvedElement

ORIGIN = "http://127.0.0.1:8000"
ALPHA = ["/login", "/search", "/member/**"]


def el(role: str, name: str, control_type: str = "button") -> ResolvedElement:
    return ResolvedElement(ref="e1", frame_path=[], bbox=BBox(x=0, y=0, w=10, h=10), control_type=control_type, role=role, name=name, handle=None)


def ctx(url: str, **kw) -> GateContext:
    base = dict(current_url=f"{ORIGIN}{url}", lease_state=LeaseState.AGENT_ACTIVE, effective_url_patterns=ALPHA)
    base.update(kw)
    return GateContext(**base)


@pytest.fixture
def gate(policy: PolicyConfig) -> ActionGate:
    return ActionGate(policy)


def test_allow_a_safe_click(gate: ActionGate) -> None:
    d = gate.check(ConcreteAction(ActionType.CLICK, target=el("button", "Search")), ctx("/search"))
    assert isinstance(d, Allow) and d.risk is RiskClass.SAFE and d.approval_by is None


def test_deny_origin_and_url_pattern_with_glob_semantics(gate: ActionGate) -> None:
    d = gate.check(ConcreteAction(ActionType.NAVIGATE, url="http://evil.example/search"), ctx("/search"))
    assert isinstance(d, Deny) and d.reason is DenyReason.ORIGIN and d.is_policy_violation
    d = gate.check(ConcreteAction(ActionType.NAVIGATE, url=f"{ORIGIN}/admin"), ctx("/search"))
    assert isinstance(d, Deny) and d.reason is DenyReason.URL_PATTERN
    # "/member/*" (one segment) would not cover the sub-account route; the effective list uses "/member/**".
    d = gate.check(ConcreteAction(ActionType.NAVIGATE, url=f"{ORIGIN}/member/204817/subaccount/new"), ctx("/search"))
    assert isinstance(d, Allow)
    d = gate.check(ConcreteAction(ActionType.NAVIGATE, url=f"{ORIGIN}/member/204817/subaccount/new"), ctx("/search", effective_url_patterns=["/login", "/search", "/member/*"]))
    assert isinstance(d, Deny) and d.reason is DenyReason.URL_PATTERN


def test_deny_action_type(policy: PolicyConfig) -> None:
    narrowed = policy.model_copy(update={"allowed_actions": [ActionType.CLICK, ActionType.FILL]})
    d = ActionGate(narrowed).check(ConcreteAction(ActionType.PRESS, target=el("textbox", "Member ID", "input"), value="Enter"), ctx("/search"))
    assert isinstance(d, Deny) and d.reason is DenyReason.ACTION_TYPE


def test_debug_state_route_is_outside_the_allowlist_by_construction(gate: ActionGate) -> None:
    for action in (ActionType.NAVIGATE, ActionType.READ):
        d = gate.check(ConcreteAction(action, url=f"{ORIGIN}/__state/member/204817"), ctx("/__state/member/204817"))
        assert isinstance(d, Deny) and d.reason is DenyReason.URL_PATTERN, action


def test_risky_needs_approval_and_url_of_commit_rule(gate: ActionGate) -> None:
    # "Continue to review" on /subaccount/new is NOT gated.
    d = gate.check(ConcreteAction(ActionType.CLICK, target=el("button", "Continue to review")), ctx("/member/204817/subaccount/new"))
    assert isinstance(d, Allow) and d.risk is RiskClass.SAFE
    # Any commit on the review page is RISKY, label-independent: CLICK and PRESS Enter.
    d = gate.check(ConcreteAction(ActionType.CLICK, target=el("button", "Confirm and open account")), ctx("/member/204817/subaccount/review", action_id="a1"))
    assert isinstance(d, NeedsApproval) and d.rule == 1 and d.action_id == "a1"
    beta = ["/portal/login", "/portal/search", "/portal/member/**"]
    d = gate.check(ConcreteAction(ActionType.PRESS, target=el("button", "Complete"), value="Enter"), ctx("/portal/member/204817/subaccount/review", effective_url_patterns=beta))
    assert isinstance(d, NeedsApproval)
    # Rule 2 (name-based) catches a relabelled confirm elsewhere under /subaccount.
    d = gate.check(ConcreteAction(ActionType.CLICK, target=el("button", "Open account now")), ctx("/member/204817/subaccount/extra"))
    assert isinstance(d, NeedsApproval) and d.rule == 2


def test_effective_risk_is_max_of_policy_and_step(gate: ActionGate) -> None:
    safe_click = ConcreteAction(ActionType.CLICK, target=el("button", "Search"))
    d = gate.check(safe_click, ctx("/search", step_risk=RiskClass.RISKY))
    assert isinstance(d, NeedsApproval) and d.rule is None  # declared RISKY, no policy rule
    risk, rule = gate.effective_risk(ConcreteAction(ActionType.CLICK, target=el("button", "Confirm and open account")), RiskClass.SAFE, f"{ORIGIN}/member/1/subaccount/review")
    assert risk is RiskClass.RISKY and rule == 1  # the artifact cannot downgrade a policy match


def test_caller_consent_and_operator_token_are_the_only_ways_through(gate: ActionGate) -> None:
    confirm = ConcreteAction(ActionType.CLICK, target=el("button", "Confirm and open account"))
    review = "/member/204817/subaccount/review"
    d = gate.check(confirm, ctx(review, approve_risky=True))
    assert isinstance(d, Allow) and d.risk is RiskClass.RISKY and d.approval_by == "caller"
    store = ApprovalStore()
    store.grant(ApprovalToken(action_id="act-7", issued_by="operator", issued_at=datetime.now(UTC)))
    d = gate.check(confirm, ctx(review, action_id="act-7", approvals=store))
    assert isinstance(d, Allow) and d.approval_by == "operator"
    # One-shot: the same action id is NeedsApproval again.
    d = gate.check(confirm, ctx(review, action_id="act-7", approvals=store))
    assert isinstance(d, NeedsApproval)
    assert store.pending() == []


def test_draft_rules(gate: ActionGate) -> None:
    confirm = ConcreteAction(ActionType.CLICK, target=el("button", "Confirm and open account"))
    d = gate.check(confirm, ctx("/member/204817/subaccount/review", artifact_status="draft", approve_risky=True))
    assert isinstance(d, Deny) and d.reason is DenyReason.DRAFT_RISKY  # effective risk, act-time
    assert ActionGate.draft_check("draft", allow_draft=False, declared_risky_step=None).reason is DenyReason.DRAFT_NOT_ALLOWED  # type: ignore[union-attr]
    assert ActionGate.draft_check("draft", allow_draft=True, declared_risky_step="click_confirm_and_open_account").reason is DenyReason.DRAFT_RISKY  # type: ignore[union-attr]
    assert ActionGate.draft_check("draft", allow_draft=True, declared_risky_step=None) is None
    assert ActionGate.draft_check("approved", allow_draft=False, declared_risky_step="x") is None
    # A safe action from a draft (with --allow-draft) is fine.
    d = gate.check(ConcreteAction(ActionType.CLICK, target=el("button", "Search")), ctx("/search", artifact_status="draft"))
    assert isinstance(d, Allow)


def test_secret_literal(gate: ActionGate) -> None:
    secrets = frozenset({"demo-pass-1"})
    pw = el("textbox", "Password", "input")
    d = gate.check(ConcreteAction(ActionType.FILL, target=pw, value="demo-pass-1", value_kind="literal"), ctx("/login", secret_values=secrets))
    assert isinstance(d, Deny) and d.reason is DenyReason.SECRET_LITERAL and d.is_policy_violation
    d = gate.check(ConcreteAction(ActionType.FILL, target=pw, value="demo-pass-1", value_kind="secret"), ctx("/login", secret_values=secrets))
    assert isinstance(d, Allow)


def test_lease_not_held_is_refused_but_not_a_policy_violation(gate: ActionGate) -> None:
    for state in (LeaseState.PAUSED, LeaseState.HUMAN_ACTIVE, LeaseState.RESUMING, LeaseState.ABANDONED):
        d = gate.check(ConcreteAction(ActionType.CLICK, target=el("button", "Search")), ctx("/search", lease_state=state))
        assert isinstance(d, Deny) and d.reason is DenyReason.LEASE_NOT_HELD and not d.is_policy_violation


def test_post_navigation_check(gate: ActionGate) -> None:
    assert gate.post_navigation_check(f"{ORIGIN}/member/204817", ALPHA) is None
    d = gate.post_navigation_check(f"{ORIGIN}/logout", ALPHA)
    assert d is not None and d.reason is DenyReason.POST_NAV_OFFLIST and d.is_policy_violation
    d = gate.post_navigation_check("http://other.example/search", ALPHA)
    assert d is not None and d.reason is DenyReason.POST_NAV_OFFLIST


def test_lease_transition_table_matches_spec_11() -> None:
    S = LeaseState
    expected = {
        (S.AGENT_ACTIVE, S.PAUSED), (S.PAUSED, S.HUMAN_ACTIVE), (S.HUMAN_ACTIVE, S.RESUMING), (S.RESUMING, S.AGENT_ACTIVE),
        (S.RESUMING, S.PAUSED), (S.PAUSED, S.AGENT_ACTIVE), (S.PAUSED, S.ABANDONED), (S.HUMAN_ACTIVE, S.ABANDONED),
    }
    assert set(LEASE_TRANSITIONS) == expected
    assert can_transition(S.PAUSED, S.AGENT_ACTIVE) and not can_transition(S.AGENT_ACTIVE, S.HUMAN_ACTIVE)
    assert not can_transition(S.ABANDONED, S.AGENT_ACTIVE) and not can_transition(S.HUMAN_ACTIVE, S.AGENT_ACTIVE)


def test_secret_resolution_from_env() -> None:
    assert resolve_secret("demo_password", {"ANTON_SECRET_DEMO_PASSWORD": "demo-pass-1"}) == "demo-pass-1"
    with pytest.raises(KeyError, match="ANTON_SECRET_DEMO_PASSWORD"):
        resolve_secret("demo_password", {})


def test_relative_navigate_url_is_joined_against_the_current_page(gate: ActionGate) -> None:
    a = ConcreteAction(ActionType.NAVIGATE, url="/member/204817")
    d = gate.check(a, ctx("/search"))
    assert isinstance(d, Allow) and a.url == f"{ORIGIN}/member/204817"
    d = gate.check(ConcreteAction(ActionType.NAVIGATE, url="/admin"), ctx("/search"))
    assert isinstance(d, Deny) and d.reason is DenyReason.URL_PATTERN


def test_risky_rules_see_the_prefix_stripped_path(policy: PolicyConfig) -> None:
    anchored = policy.model_copy(update={"risky_rules": [policy.risky_rules[0].model_copy(update={"url_matches": "^/member/.*/subaccount/review$"})]})
    g = ActionGate(anchored)
    beta = ["/portal/login", "/portal/search", "/portal/member/**"]
    d = g.check(ConcreteAction(ActionType.CLICK, target=el("button", "Complete")), ctx("/portal/member/1/subaccount/review", effective_url_patterns=beta, route_prefix="/portal"))
    assert isinstance(d, NeedsApproval)
    d = g.check(ConcreteAction(ActionType.CLICK, target=el("button", "Complete")), ctx("/portal/member/1/subaccount/review", effective_url_patterns=beta))
    assert isinstance(d, Allow)  # without the prefix the anchored rule does not match: the engine must pass it
