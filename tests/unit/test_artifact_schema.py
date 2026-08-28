"""SPEC §16 "Schema": round-trips, discriminated unions, and the §4 validators."""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from anton.schema.artifact import (
    ActionType,
    AllOf,
    BBoxLocator,
    BusinessDetector,
    CapabilityArtifact,
    Checked,
    Dismiss,
    ElementFingerprint,
    ElementVisible,
    Escalate,
    ExactTextLocator,
    FieldValue,
    HardFailureDetector,
    LabelRelativeLocator,
    LiteralValue,
    LocatorCandidate,
    LocatorLadder,
    LocatorTier,
    OutcomeDetector,
    ParamRef,
    Predicate,
    RecoverableDetector,
    RecoveryAction,
    RetryStep,
    RiskClass,
    RoleNameLocator,
    RunFlow,
    SecretRef,
    StepSpec,
    TextVisible,
    TitleMatches,
    UrlMatches,
    ValueRef,
    content_hash,
)
from anton.schema.result import ReplayResult, ReplayStatus, StepResult, StepStatus
from tests.conftest import ladder, make_artifact, make_steps, row_cell, sig


def test_artifact_json_round_trip() -> None:
    a = make_artifact()
    text = a.model_dump_json()
    back = CapabilityArtifact.model_validate_json(text)
    assert back == a
    assert json.loads(text)["steps"][0]["target"]["candidates"][0]["kind"] == "role_name"


def test_result_json_round_trip() -> None:
    from datetime import UTC, datetime

    r = ReplayResult(
        run_id="r1", capability_id="servicing.read_savings_balance", version="1.0.0", artifact_sha256="0" * 64,
        status=ReplayStatus.SUCCESS, outputs={"savings_balance": 1234.56},
        steps=[StepResult(step_id="__preconditions__", status=StepStatus.PASSED, attempts=1, duration_ms=10)],
        started_at=datetime(2026, 8, 28, tzinfo=UTC), finished_at=datetime(2026, 8, 28, tzinfo=UTC), redacted_view=True,
    )
    assert ReplayResult.model_validate_json(r.model_dump_json()) == r


@pytest.mark.parametrize(
    "adapter, obj",
    [
        (TypeAdapter(LocatorCandidate), RoleNameLocator(tier=1, role="button", name="Search", rationale="r")),
        (TypeAdapter(LocatorCandidate), row_cell()),
        (TypeAdapter(LocatorCandidate), ExactTextLocator(tier=2, text="No member found", rationale="r")),
        (TypeAdapter(LocatorCandidate), BBoxLocator(tier=3, x=0.1, y=0.2, w=0.3, h=0.1, rationale="r")),
        (TypeAdapter(Predicate), UrlMatches(regex="^/x$")),
        (TypeAdapter(Predicate), FieldValue(ladder=ladder(), expected=ParamRef(name="member_id"))),
        (TypeAdapter(Predicate), Checked(ladder=ladder(), expected=False)),
        (TypeAdapter(Predicate), AllOf(predicates=[UrlMatches(regex="^/login"), TextVisible(regex="expired")])),
        (TypeAdapter(ValueRef), LiteralValue(value="x")),
        (TypeAdapter(ValueRef), ParamRef(name="member_id")),
        (TypeAdapter(ValueRef), SecretRef(name="DEMO_PASSWORD")),
        (TypeAdapter(RecoveryAction), Dismiss(target=ladder())),
        (TypeAdapter(RecoveryAction), RetryStep(max_attempts=3)),
        (TypeAdapter(RecoveryAction), RunFlow(flow_id="login")),
        (TypeAdapter(RecoveryAction), Escalate()),
        (TypeAdapter(OutcomeDetector), RecoverableDetector(id="x", when=[UrlMatches(regex="a")], recovery=Escalate())),
    ],
)
def test_discriminated_union_round_trip(adapter: TypeAdapter, obj) -> None:
    dumped = adapter.dump_json(obj)
    assert adapter.validate_json(dumped) == obj
    payload = json.loads(dumped)
    assert payload.get("kind") or payload.get("arm")


def test_json_schema_uses_discriminators() -> None:
    schema = CapabilityArtifact.model_json_schema()
    text = json.dumps(schema)
    assert '"discriminator"' in text
    assert "role_name" in text and "all_of" in text and "RECOVERABLE" in text


@pytest.mark.parametrize(
    "cls, kwargs",
    [
        (RoleNameLocator, dict(tier=2, role="button", name="x", rationale="r")),
        (LabelRelativeLocator, dict(tier=1, relation="right_of_label", anchor_text="x", rationale="r")),
        (ExactTextLocator, dict(tier=3, text="x", rationale="r")),
        (BBoxLocator, dict(tier=1, x=0, y=0, w=0.1, h=0.1, rationale="r")),
    ],
)
def test_kind_tier_validator(cls, kwargs) -> None:
    with pytest.raises(ValidationError, match="must be tier"):
        cls(**kwargs)


def test_row_cell_requires_col_header() -> None:
    with pytest.raises(ValidationError, match="row_cell requires col_header"):
        LabelRelativeLocator(tier=2, relation="row_cell", anchor_text="Savings", rationale="r")


def test_ladder_tiers_non_decreasing_and_rationale_required() -> None:
    fp = ElementFingerprint(control_type="td")
    with pytest.raises(ValidationError, match="non-decreasing"):
        LocatorLadder(candidates=[BBoxLocator(tier=3, x=0, y=0, w=0.1, h=0.1, rationale="r"), RoleNameLocator(tier=1, role="cell", name="x", rationale="r")], fingerprint=fp)
    with pytest.raises(ValidationError):
        RoleNameLocator(tier=1, role="cell", name="x", rationale="")


def test_risky_step_rules() -> None:
    target = ladder("button", "Confirm and open account")
    post = sig(UrlMatches(regex="confirmation"))
    ok = StepSpec(id="click_confirm_and_open_account", description="d", action=ActionType.CLICK, target=target, risk=RiskClass.RISKY, retries=0, postcondition=post)
    assert ok.requires_approval is True
    with pytest.raises(ValidationError, match="retries == 0"):
        StepSpec(id="click_confirm", description="d", action=ActionType.CLICK, target=target, risk=RiskClass.RISKY, postcondition=post)
    with pytest.raises(ValidationError, match="RetryStep"):
        StepSpec(
            id="click_confirm", description="d", action=ActionType.CLICK, target=target, risk=RiskClass.RISKY, retries=0, postcondition=post,
            detectors=[RecoverableDetector(id="slow", when=[UrlMatches(regex="x")], recovery=RetryStep())],
        )
    # requires_approval is derived from risk: an inconsistent input value is forced, not trusted.
    forced = StepSpec(id="click_search", description="d", action=ActionType.CLICK, target=target, requires_approval=True, postcondition=post)
    assert forced.requires_approval is False


def test_fill_requires_field_value_on_target_and_read_is_rejected() -> None:
    target = ladder("textbox", "Member ID", control_type="input")
    with pytest.raises(ValidationError, match="FieldValue/Checked"):
        StepSpec(id="fill_member_id", description="d", action=ActionType.FILL, target=target, value=ParamRef(name="member_id"), postcondition=sig(UrlMatches(regex="x")))
    with pytest.raises(ValidationError, match="discovery-only"):
        StepSpec(id="read_balance", description="d", action=ActionType.READ, target=target, postcondition=sig(UrlMatches(regex="x")))
    with pytest.raises(ValidationError):  # reserved ids also fail the id pattern
        StepSpec(id="__outputs__", description="d", action=ActionType.CLICK, target=target, postcondition=sig(UrlMatches(regex="x")))


def test_click_requires_target_and_element_visible_needs_ladder() -> None:
    with pytest.raises(ValidationError, match="requires a target"):
        StepSpec(id="click_x", description="d", action=ActionType.CLICK, postcondition=sig(UrlMatches(regex="x")))
    with pytest.raises(ValidationError):
        ElementVisible(ladder="not a ladder")  # type: ignore[arg-type]


def test_artifact_level_rules() -> None:
    with pytest.raises(ValidationError, match="approval block"):
        make_artifact(status="approved")
    steps = make_steps()
    with pytest.raises(ValidationError, match="unique"):
        make_artifact(steps=[steps[0], steps[0]])
    with pytest.raises(ValidationError, match="undeclared params"):
        make_artifact(inputs=[])
    with pytest.raises(ValidationError, match="UPPER_SNAKE"):
        make_artifact(business_outcomes={"not-a-code": "x"})


def test_content_hash_ignores_status_and_approval_but_not_content() -> None:
    a = make_artifact()
    h = content_hash(a)
    assert len(h) == 64
    approved = a.model_copy(update={"status": "approved"})
    assert content_hash(approved) == h
    edited = a.model_copy(update={"description": "changed"})
    assert content_hash(edited) != h


def test_locator_tier_is_int_enum_and_serializes_as_int() -> None:
    assert int(LocatorTier.VISUAL) == 3
    assert json.loads(ladder().model_dump_json())["candidates"][0]["tier"] == 1


# --- audit additions: canonical hash form, remaining union arms, param-reference walk ---


def test_content_hash_is_sha256_of_sorted_key_compact_json_with_neutral_status() -> None:
    import hashlib

    a = make_artifact()
    neutral = a.model_copy(update={"status": "draft", "approval": None}).model_dump(mode="json")
    expected = hashlib.sha256(json.dumps(neutral, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert content_hash(a) == expected
    # Independent of source key order / indentation.
    reparsed = CapabilityArtifact.model_validate(json.loads(json.dumps(a.model_dump(mode="json"), indent=2, sort_keys=True)))
    assert content_hash(reparsed) == expected


@pytest.mark.parametrize(
    "adapter, obj",
    [
        (TypeAdapter(Predicate), TitleMatches(regex="500")),
        (TypeAdapter(Predicate), TextVisible(regex="No member found")),
        (TypeAdapter(Predicate), ElementVisible(ladder=ladder())),
        (TypeAdapter(LocatorCandidate), LabelRelativeLocator(tier=2, relation="right_of_label", anchor_text="SSN", rationale="r")),
        (TypeAdapter(LocatorCandidate), LabelRelativeLocator(tier=2, relation="below_label", anchor_text="Nickname", rationale="r")),
        (TypeAdapter(OutcomeDetector), BusinessDetector(id="nf", when=[TextVisible(regex="No member")], outcome_code="MEMBER_NOT_FOUND", detail_locator=ladder())),
        (TypeAdapter(OutcomeDetector), HardFailureDetector(id="app_error", when=[TitleMatches(regex="500")], reason="application error page")),
    ],
)
def test_remaining_union_arms_round_trip(adapter: TypeAdapter, obj) -> None:
    assert adapter.validate_json(adapter.dump_json(obj)) == obj


def test_param_reference_walk_covers_field_value_and_tokens() -> None:
    from anton.schema.artifact import referenced_param_names

    a = make_artifact()
    assert referenced_param_names(a.model_dump(mode="json", exclude={"inputs"})) == {"member_id"}
    tokened = a.model_copy(update={"success": sig(TextVisible(regex="Member $param:member_id"), TextVisible(regex="$param:ghost"))})
    with pytest.raises(ValidationError, match="undeclared params.*ghost"):
        CapabilityArtifact.model_validate(tokened.model_dump(mode="json"))
    steps = make_steps()
    bad = steps[0].model_copy(update={"postcondition": sig(FieldValue(ladder=steps[0].target, expected=ParamRef(name="nope")))})
    with pytest.raises(ValidationError, match="nope"):
        make_artifact(steps=[bad, steps[1]])


def test_navigate_accepts_param_ref_and_profile_round_trips() -> None:
    from anton.schema.app_profile import AppProfile
    from tests.conftest import make_profile

    nav = StepSpec(id="navigate_member", description="d", action=ActionType.NAVIGATE, value=ParamRef(name="member_id"), postcondition=sig(UrlMatches(regex="^/member/[^/]+$")))
    assert nav.value is not None
    p = make_profile()
    assert AppProfile.model_validate(p.model_dump(mode="json")) == p
