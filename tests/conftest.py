"""Shared builders: a minimal, valid capability artifact + profile that mirror SPEC §6/§13."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from anton.policy.gate import PolicyConfig, load_policy
from anton.schema.app_profile import AppProfile, MaskSpec, StepOverride, TenantOverlay
from anton.schema.artifact import (
    ActionType,
    AllOf,
    BBoxLocator,
    BusinessDetector,
    CapabilityArtifact,
    Dismiss,
    ElementFingerprint,
    ElementVisible,
    ExactTextLocator,
    ExtractionSpec,
    FieldValue,
    HardFailureDetector,
    LabelRelativeLocator,
    LiteralValue,
    LocatorLadder,
    LocatorTier,
    OutcomeDetector,
    OutputSpec,
    OutputType,
    ParamRef,
    ParamSpec,
    ParamType,
    Predicate,
    Provenance,
    RecordedAgainst,
    RecoverableDetector,
    RoleNameLocator,
    RunFlow,
    SecretRef,
    StateSignature,
    StepSpec,
    TextVisible,
    TitleMatches,
    UrlMatches,
)


def ladder(
    role: str = "button",
    name: str = "Search",
    *,
    frame: list[str] | None = None,
    control_type: str = "button",
    with_snippets: bool = True,
    tier2: LabelRelativeLocator | ExactTextLocator | None = None,
    bbox: bool = True,
) -> LocatorLadder:
    cands: list = [
        RoleNameLocator(
            tier=LocatorTier.SEMANTIC, role=role, name=name, rationale=f"accessible name {name!r} is stable", frame_path=frame or []
        )
    ]
    if tier2 is not None:
        cands.append(tier2)
    if bbox:
        cands.append(BBoxLocator(tier=LocatorTier.VISUAL, x=0.1, y=0.1, w=0.2, h=0.05, rationale="last resort", frame_path=frame or []))
    fp = ElementFingerprint(role=role, control_type=control_type, name_snippet=name if with_snippets else None)
    return LocatorLadder(candidates=cands, fingerprint=fp)


def row_cell(anchor: str = "Savings", col: str = "Balance", *, frame: list[str] | None = None) -> LabelRelativeLocator:
    return LabelRelativeLocator(
        tier=LocatorTier.ANCHORED,
        relation="row_cell",
        anchor_text=anchor,
        anchor_col_header="Type",
        col_header=col,
        rationale="row/column anchoring survives column reordering",
        frame_path=frame or [],
    )


def sig(*preds: Predicate) -> StateSignature:
    return StateSignature(all_of=list(preds))


PANE = ["iframe[name=detail]"]


def signout_ladder() -> LocatorLadder:
    return ladder("link", "Sign out", control_type="a")


def balance_ladder() -> LocatorLadder:
    """Extraction target: no snippets in the fingerprint (a balance cell's name is its value)."""
    return LocatorLadder(
        candidates=[row_cell(frame=PANE)],
        fingerprint=ElementFingerprint(role="cell", control_type="td"),
    )


def make_steps() -> list[StepSpec]:
    member_field = ladder("textbox", "Member ID", control_type="input")
    return [
        StepSpec(
            id="fill_member_id",
            description="Type the member id into the search field",
            action=ActionType.FILL,
            target=member_field,
            value=ParamRef(name="member_id"),
            postcondition=sig(FieldValue(ladder=member_field, expected=ParamRef(name="member_id"))),
        ),
        StepSpec(
            id="click_search",
            description="Submit the search",
            action=ActionType.CLICK,
            target=ladder("button", "Search"),
            postcondition=sig(UrlMatches(regex="^/member/[^/]+$"), TextVisible(regex="Member Profile")),
        ),
    ]


def make_artifact(**overrides: Any) -> CapabilityArtifact:
    data: dict[str, Any] = dict(
        capability_id="servicing.read_savings_balance",
        name="Read savings balance",
        description="Look up a member and read the current savings balance.",
        version="1.0.0",
        app_profile="profiles/servicing.yaml",
        preconditions=sig(UrlMatches(regex="^/search$"), ElementVisible(ladder=signout_ladder())),
        inputs=[ParamSpec(name="member_id", type=ParamType.STRING, description="Member id", pattern="^[0-9]+$")],
        outputs=[
            OutputSpec(
                name="savings_balance",
                type=OutputType.NUMBER,
                description="Current savings balance",
                extraction=ExtractionSpec(locator=balance_ladder(), attr="text", normalize="money"),
            )
        ],
        business_outcomes={
            "MEMBER_NOT_FOUND": "No member with that id exists",
            "VALIDATION_ERROR": "The app rejected an input",
            "PERMISSION_DENIED": "The operator lacks permission for this action",
        },
        steps=make_steps(),
        success=sig(UrlMatches(regex="^/member/[^/]+$"), ElementVisible(ladder=balance_ladder())),
        provenance=Provenance(
            origin="hand_authored",
            created_at=datetime(2026, 8, 28, tzinfo=UTC),
            recorded_against=RecordedAgainst(app_id="servicing", tenant_id="alpha", app_version="1", entry_url="http://localhost:8000/search"),
        ),
    )
    data.update(overrides)
    return CapabilityArtifact.model_validate(data)


def make_login_flow() -> list[StepSpec]:
    user = ladder("textbox", "Username", control_type="input")
    pw = ladder("textbox", "Password", control_type="input")
    return [
        StepSpec(
            id="fill_username", description="username", action=ActionType.FILL, target=user,
            value=LiteralValue(value="ops_user"),
            postcondition=sig(FieldValue(ladder=user, expected=LiteralValue(value="ops_user"))),
        ),
        StepSpec(
            id="fill_password", description="password", action=ActionType.FILL, target=pw,
            value=SecretRef(name="DEMO_PASSWORD"),
            postcondition=sig(FieldValue(ladder=pw, expected=SecretRef(name="DEMO_PASSWORD"))),
        ),
        StepSpec(
            id="click_sign_in", description="sign in", action=ActionType.CLICK, target=ladder("button", "Sign in"),
            postcondition=sig(ElementVisible(ladder=signout_ladder())),
        ),
    ]


def make_detectors() -> list[OutcomeDetector]:
    dismiss = ladder("button", "Dismiss")
    return [
        RecoverableDetector(
            id="session_expired",
            when=[AllOf(predicates=[UrlMatches(regex="^/login"), TextVisible(regex="session has expired")])],
            recovery=RunFlow(flow_id="login"),
        ),
        RecoverableDetector(id="announcement_modal", when=[ElementVisible(ladder=dismiss)], recovery=Dismiss(target=dismiss)),
        BusinessDetector(id="member_not_found", when=[TextVisible(regex="No member found")], outcome_code="MEMBER_NOT_FOUND"),
        BusinessDetector(id="validation_error", when=[TextVisible(regex="Please correct the errors below")], outcome_code="VALIDATION_ERROR"),
        BusinessDetector(id="permission_denied", when=[TextVisible(regex="do not have permission")], outcome_code="PERMISSION_DENIED"),
        HardFailureDetector(id="app_error", when=[TitleMatches(regex="500")], reason="application error page"),
    ]


def make_profile(**overrides: Any) -> AppProfile:
    data: dict[str, Any] = dict(
        app_id="servicing",
        tenant_id="alpha",
        app_version="1",
        entry_url="http://localhost:8000/search",
        route_prefix="",
        allowed_url_patterns=["/login", "/search", "/member/**"],
        flows={"login": make_login_flow()},
        global_detectors=make_detectors(),
        sensitive_elements=[
            MaskSpec(
                id="ssn_cell",
                ladder=LocatorLadder(
                    candidates=[LabelRelativeLocator(tier=LocatorTier.ANCHORED, relation="right_of_label", anchor_text="SSN", rationale="value cell right of the SSN label", frame_path=PANE)],
                    fingerprint=ElementFingerprint(role="cell", control_type="td"),
                ),
                applies_to="^/member/[^/]+$",
            )
        ],
    )
    data.update(overrides)
    return AppProfile.model_validate(data)


def make_overlay(**overrides: Any) -> TenantOverlay:
    data: dict[str, Any] = dict(
        overlay_id="beta",
        tenant_id="beta",
        app_id="servicing",
        base_capability_id="servicing.read_savings_balance",
        entry_url="http://localhost:8000/portal/search",
        route_prefix="/portal",
        step_overrides={"fill_member_id": StepOverride(target=ladder("textbox", "Customer Number", control_type="input"))},
        output_overrides={
            "savings_balance": ExtractionSpec(locator=balance_ladder(), attr="text", normalize="money"),
        },
    )
    data.update(overrides)
    return TenantOverlay.model_validate(data)


@pytest.fixture
def policy() -> PolicyConfig:
    return load_policy(env={"ANTON_TARGET_ORIGIN": "http://127.0.0.1:8000"})


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    """A profile on disk plus an artifact pointing at it (absolute path is allowed)."""
    profile_path = tmp_path / "servicing.yaml"
    profile_path.write_text(yaml.safe_dump(make_profile().model_dump(mode="json")), encoding="utf-8")
    artifact = make_artifact(app_profile=str(profile_path))
    artifact_path = tmp_path / "read_savings_balance.json"
    artifact_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    overlay_path = tmp_path / "beta.yaml"
    overlay_path.write_text(yaml.safe_dump(make_overlay().model_dump(mode="json")), encoding="utf-8")
    return {"profile": profile_path, "artifact": artifact_path, "overlay": overlay_path, "dir": tmp_path}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
