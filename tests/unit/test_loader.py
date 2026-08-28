"""SPEC §16 "Schema … loader": closure, approval, overlay narrowing, schema major, forbidden extraction, glob subset."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from anton.policy.gate import PolicyConfig
from anton.schema.app_profile import StepOverride
from anton.schema.artifact import (
    Approval,
    BusinessDetector,
    CapabilityArtifact,
    ElementFingerprint,
    ExtractionSpec,
    LabelRelativeLocator,
    LocatorLadder,
    LocatorTier,
    OutputSpec,
    OutputType,
    TextVisible,
    content_hash,
)
from anton.schema.loader import (
    LoaderError,
    apply_overlay,
    check_closure,
    check_forbidden_extraction,
    check_profile_subset,
    check_schema_version,
    glob_covers,
    glob_match,
    load_artifact,
    load_bundle,
    normalize_approval,
)
from tests.conftest import PANE, ladder, make_artifact, make_overlay, make_profile, make_steps, write_json


def approve(a: CapabilityArtifact) -> CapabilityArtifact:
    return a.model_copy(update={"status": "approved", "approval": Approval(approved_at=datetime(2026, 8, 28, tzinfo=UTC), approved_by="t", artifact_sha256=content_hash(a))})


# --- closure ------------------------------------------------------------------------------


def test_closure_passes_when_every_reachable_code_is_declared() -> None:
    check_closure(make_artifact(), make_profile())


def test_closure_rejects_missing_code_from_profile_detector() -> None:
    a = make_artifact(business_outcomes={"MEMBER_NOT_FOUND": "x"})
    with pytest.raises(LoaderError, match=r"not declared.*VALIDATION_ERROR"):
        check_closure(a, make_profile())


def test_closure_rejects_missing_code_from_step_detector() -> None:
    steps = make_steps()
    steps[1] = steps[1].model_copy(update={"detectors": [BusinessDetector(id="locked", when=[TextVisible(regex="locked")], outcome_code="ACCOUNT_LOCKED")]})
    with pytest.raises(LoaderError, match="ACCOUNT_LOCKED"):
        check_closure(make_artifact(steps=steps), make_profile())


def test_overlay_may_not_introduce_an_outcome_code() -> None:
    ov = make_overlay(step_overrides={"click_search": StepOverride(extra_detectors=[BusinessDetector(id="x", when=[TextVisible(regex="x")], outcome_code="NEW_CODE")])})
    with pytest.raises(LoaderError, match="introduces outcome code"):
        apply_overlay(make_artifact(), ov)


# --- approval -----------------------------------------------------------------------------


def test_approval_hash_mismatch_is_refused() -> None:
    a = approve(make_artifact())
    edited = a.model_copy(update={"description": "edited after approval"})
    with pytest.raises(LoaderError, match="approval hash mismatch"):
        normalize_approval(edited)


def test_stale_approval_on_a_draft_is_ignored() -> None:
    a = approve(make_artifact())
    draft_copy = a.model_copy(update={"status": "draft"})
    out = normalize_approval(draft_copy)
    assert out.status == "draft" and out.approval is None


def test_valid_approval_passes_and_hash_is_stable_across_file_round_trip(workspace) -> None:
    a = approve(make_artifact(app_profile=str(workspace["profile"])))
    workspace["artifact"].write_text(a.model_dump_json(indent=2), encoding="utf-8")
    loaded = load_artifact(workspace["artifact"])
    assert loaded.status == "approved" and loaded.approval is not None


# --- schema version -----------------------------------------------------------------------


def test_schema_major_mismatch_is_refused() -> None:
    with pytest.raises(LoaderError, match="major differs"):
        check_schema_version(make_artifact(schema_version="2.0"))
    check_schema_version(make_artifact(schema_version="1.7"))


# --- forbidden extraction ------------------------------------------------------------------


def test_forbidden_extract_rejects_ssn_output(policy: PolicyConfig) -> None:
    ssn = OutputSpec(
        name="ssn",
        type=OutputType.STRING,
        description="never",
        extraction=ExtractionSpec(
            locator=LocatorLadder(
                candidates=[LabelRelativeLocator(tier=LocatorTier.ANCHORED, relation="right_of_label", anchor_text="SSN", rationale="r", frame_path=PANE)],
                fingerprint=ElementFingerprint(control_type="td"),
            ),
            attr="text",
        ),
    )
    a = make_artifact()
    a = a.model_copy(update={"outputs": [*a.outputs, ssn]})
    with pytest.raises(LoaderError, match="forbidden element"):
        check_forbidden_extraction(a, policy.forbidden_extract_patterns)
    check_forbidden_extraction(make_artifact(), policy.forbidden_extract_patterns)


# --- overlay narrowing ---------------------------------------------------------------------


def test_overlay_retargets_and_reports_sources() -> None:
    a = make_artifact()
    eff, sources = apply_overlay(a, make_overlay())
    assert sources.step_targets == {"fill_member_id"} and sources.outputs == {"savings_balance"}
    fill = next(s for s in eff.steps if s.id == "fill_member_id")
    assert fill.target is not None and fill.target.candidates[0].name == "Customer Number"  # type: ignore[union-attr]
    assert [s.id for s in eff.steps] == [s.id for s in a.steps]
    assert eff.business_outcomes == a.business_outcomes


def test_overlay_cannot_add_steps_or_unknown_outputs() -> None:
    with pytest.raises(LoaderError, match="cannot add steps"):
        apply_overlay(make_artifact(), make_overlay(step_overrides={"click_extra": StepOverride(wait_timeout_ms=9000)}))
    with pytest.raises(LoaderError, match="unknown outputs"):
        apply_overlay(make_artifact(), make_overlay(output_overrides={"nope": ExtractionSpec(locator=ladder(), attr="text")}))


def test_overlay_retargeting_a_fill_step_retargets_its_field_value_predicate() -> None:
    """A FILL's FieldValue must stay on the field it fills: when an overlay supplies a new target
    without a postcondition, the loader moves predicates that pointed at the old target."""
    new_target = ladder("textbox", "Customer Number", control_type="input")
    ov = make_overlay(step_overrides={"fill_member_id": StepOverride(target=new_target)})
    eff, _ = apply_overlay(make_artifact(), ov)
    fill = eff.steps[0]
    assert fill.target == new_target
    assert any(p.kind == "field_value" and p.ladder == new_target for p in fill.postcondition.all_of)


def test_overlay_wrong_capability_is_refused() -> None:
    with pytest.raises(LoaderError, match="targets"):
        apply_overlay(make_artifact(), make_overlay(base_capability_id="servicing.other"))


# --- glob semantics -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern, path, expected",
    [
        ("/member/*", "/member/204817", True),
        ("/member/*", "/member/204817/subaccount/new", False),
        ("/member/**", "/member/204817/subaccount/new", True),
        ("/member/**", "/member", True),
        ("/login", "/login", True),
        ("/login", "/login/extra", False),
        ("/portal/member/**", "/member/1", False),
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


def test_glob_covers_patterns_not_just_paths() -> None:
    assert glob_covers("/member/**", "/member/*")
    assert glob_covers("/member/**", "/member/*/subaccount/*")
    assert not glob_covers("/member/*", "/member/**")


def test_profile_subset_rule(policy: PolicyConfig) -> None:
    check_profile_subset(["/login", "/search", "/member/**"], policy)
    check_profile_subset(["/portal/login", "/portal/search", "/portal/member/**"], policy)
    with pytest.raises(LoaderError, match="not covered"):
        check_profile_subset(["/admin"], policy)


# --- bundle ---------------------------------------------------------------------------------


def test_load_bundle_end_to_end_with_and_without_overlay(workspace, policy: PolicyConfig) -> None:
    b = load_bundle(workspace["artifact"], policy=policy)
    assert b.tenant_id == "alpha" and b.route_prefix == "" and b.entry_url == "http://localhost:8000/search"
    assert b.effective_allowed_url_patterns == ["/login", "/search", "/member/**"]

    b2 = load_bundle(workspace["artifact"], overlay_path=workspace["overlay"], policy=policy, url="http://127.0.0.1:5123")
    assert b2.tenant_id == "beta" and b2.route_prefix == "/portal"
    assert b2.entry_url == "http://127.0.0.1:5123/portal/search"  # origin-only --url takes the overlay's path
    assert b2.effective_allowed_url_patterns == ["/portal/login", "/portal/search", "/portal/member/**"]
    assert "fill_member_id" in b2.sources.step_targets


def test_load_bundle_refuses_bad_json_and_missing_file(tmp_path, policy: PolicyConfig) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(LoaderError, match="invalid JSON"):
        load_bundle(bad, policy=policy)
    with pytest.raises(LoaderError, match="not found"):
        load_bundle(tmp_path / "missing.json", policy=policy)


def test_load_bundle_reports_first_validation_error_on_one_line(workspace, policy: PolicyConfig) -> None:
    data = make_artifact(app_profile=str(workspace["profile"])).model_dump(mode="json")
    data["steps"][1]["risk"] = "RISKY"  # retries stays 1 → validator error
    write_json(workspace["artifact"], data)
    with pytest.raises(LoaderError, match="retries == 0"):
        load_bundle(workspace["artifact"], policy=policy)


# --- audit additions: §16 "rejects lowered risk / removed detectors", flows, --url composition ---


@pytest.mark.parametrize(
    "bad_key, value",
    [("risk", "SAFE"), ("requires_approval", False), ("detectors", []), ("action", "CLICK"), ("value", {"kind": "literal", "value": "x"}), ("retries", 5)],
)
def test_overlay_cannot_touch_risk_action_value_or_detectors(bad_key: str, value) -> None:
    """Lowered risk / removed detectors / changed action are unrepresentable by StepOverride:
    the model forbids unknown keys, and this pins that guarantee (decisions.md P1-14)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        StepOverride.model_validate({bad_key: value})


def test_overlay_keeps_every_base_detector_and_never_lowers_risk() -> None:
    steps = make_steps()
    steps[1] = steps[1].model_copy(update={"detectors": [BusinessDetector(id="nf", when=[TextVisible(regex="No member found")], outcome_code="MEMBER_NOT_FOUND")]})
    base = make_artifact(steps=steps)
    ov = make_overlay(step_overrides={"click_search": StepOverride(extra_detectors=[BusinessDetector(id="pd", when=[TextVisible(regex="permission")], outcome_code="PERMISSION_DENIED")])})
    eff, sources = apply_overlay(base, ov)
    base_ids = {d.id for d in base.steps[1].detectors}
    assert base_ids <= {d.id for d in eff.steps[1].detectors}
    assert [s.risk for s in eff.steps] == [s.risk for s in base.steps]
    assert sources.step_extra_detectors == {"click_search"}


def test_check_flows_rejects_unknown_flow_ids() -> None:
    from anton.schema.artifact import RecoverableDetector, RunFlow, UrlMatches
    from anton.schema.loader import check_flows

    check_flows(make_artifact(), make_profile())
    with pytest.raises(LoaderError, match=r"flows \['sso'\] are referenced"):
        check_flows(make_artifact(requires_flows=["login", "sso"]), make_profile())
    dets = [*make_profile().global_detectors, RecoverableDetector(id="relogin", when=[UrlMatches(regex="x")], recovery=RunFlow(flow_id="logn"))]
    with pytest.raises(LoaderError, match="logn"):
        check_flows(make_artifact(), make_profile(global_detectors=dets))


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://127.0.0.1:5123", "http://127.0.0.1:5123/portal/search"),
        ("http://127.0.0.1:5123/", "http://127.0.0.1:5123/portal/search"),
        ("http://127.0.0.1:5123/custom/entry", "http://127.0.0.1:5123/custom/entry"),
    ],
)
def test_entry_url_composition(url: str, expected: str) -> None:
    from anton.schema.loader import _compose_entry_url

    assert _compose_entry_url(url, make_overlay(), make_profile()) == expected


def test_entry_url_rejects_non_http_and_origin_with_query() -> None:
    from anton.schema.loader import _compose_entry_url

    with pytest.raises(LoaderError, match="absolute http"):
        _compose_entry_url("localhost:5123", None, make_profile())
    with pytest.raises(LoaderError, match="query or fragment"):
        _compose_entry_url("http://localhost:5123?x=1", None, make_profile())


def test_glob_trailing_slash_and_fragment_do_not_matter() -> None:
    assert glob_match("/member/**", "/member/") and glob_match("/login", "/login/")
    assert glob_match("/member/**", "/member/1#top") and glob_match("/search", "/search?q=1")
