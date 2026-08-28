"""SPEC §5: the exit code is a pure function of the result, for both result models."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product

import pytest
from pydantic import ValidationError

from anton.schema.result import (
    EXIT_BLOCKED,
    EXIT_BUSINESS_OUTCOME,
    EXIT_FAILED,
    EXIT_POLICY_VIOLATION,
    EXIT_SUCCESS,
    EXIT_USAGE,
    BlockedDetail,
    BlockedKind,
    DiscoveryResult,
    FailureDetail,
    FailureKind,
    ReplayResult,
    ReplayStatus,
    exit_code,
)

T0 = datetime(2026, 8, 28, tzinfo=UTC)


def replay(status: ReplayStatus, *, fkind: FailureKind | None = None, bkind: BlockedKind | None = None) -> ReplayResult:
    kw: dict = dict(
        run_id="r", capability_id="servicing.x", version="1.0.0", artifact_sha256="0" * 64,
        status=status, started_at=T0, finished_at=T0, redacted_view=True,
    )
    if status is ReplayStatus.SUCCESS:
        kw["outputs"] = {"x": "1"}
    elif status is ReplayStatus.BUSINESS_OUTCOME:
        kw.update(outcome_code="MEMBER_NOT_FOUND", outcome_step_id="click_search", outcome_detector_id="member_not_found")
    elif status is ReplayStatus.FAILED:
        assert fkind is not None
        kw["failure"] = FailureDetail(kind=fkind, step_id="click_search", observed_summary="x")
    elif status is ReplayStatus.BLOCKED:
        assert bkind is not None
        kw["blocked"] = BlockedDetail(kind=bkind, step_id="click_confirm_and_open_account", lease_state="PAUSED")
    return ReplayResult(**kw)


def discovery(status: str, *, fkind: FailureKind | None = None, bkind: BlockedKind | None = None) -> DiscoveryResult:
    kw: dict = dict(run_id="r", goal="g", status=status, turns=3, started_at=T0, finished_at=T0)
    if status == "SUCCESS":
        kw["artifact_path"] = "artifacts/x.json"
    if status == "FAILED":
        kw["failure_kind"] = fkind
    if status == "BLOCKED":
        kw["blocked"] = BlockedDetail(kind=bkind or BlockedKind.APPROVAL_REQUIRED, step_id="s", lease_state="PAUSED")
    return DiscoveryResult(**kw)


EXPECTED_REPLAY = {
    (ReplayStatus.SUCCESS, None, None): EXIT_SUCCESS,
    (ReplayStatus.BUSINESS_OUTCOME, None, None): EXIT_BUSINESS_OUTCOME,
    **{(ReplayStatus.BLOCKED, None, b): EXIT_BLOCKED for b in BlockedKind},
    **{(ReplayStatus.FAILED, f, None): (EXIT_POLICY_VIOLATION if f is FailureKind.POLICY_VIOLATION else EXIT_FAILED) for f in FailureKind},
}


@pytest.mark.parametrize("status, fkind, bkind", list(EXPECTED_REPLAY))
def test_replay_exit_code_is_pure_function(status, fkind, bkind) -> None:
    assert exit_code(replay(status, fkind=fkind, bkind=bkind)) == EXPECTED_REPLAY[(status, fkind, bkind)]


def test_every_replay_status_and_kind_is_covered() -> None:
    statuses = {s for s, _, _ in EXPECTED_REPLAY}
    assert statuses == set(ReplayStatus)
    assert {f for _, f, _ in EXPECTED_REPLAY if f} == set(FailureKind)
    assert {b for _, _, b in EXPECTED_REPLAY if b} == set(BlockedKind)


@pytest.mark.parametrize(
    "status, fkind, expected",
    [
        ("SUCCESS", None, EXIT_SUCCESS),
        ("GAVE_UP", None, EXIT_FAILED),
        ("BLOCKED", None, EXIT_BLOCKED),
        *[("FAILED", f, EXIT_POLICY_VIOLATION if f is FailureKind.POLICY_VIOLATION else EXIT_FAILED) for f in FailureKind],
    ],
)
def test_discovery_exit_code_is_pure_function(status, fkind, expected) -> None:
    assert exit_code(discovery(status, fkind=fkind)) == expected


def test_pre_run_exit_codes_are_distinct_constants() -> None:
    assert EXIT_USAGE == 2 and EXIT_POLICY_VIOLATION == 30
    assert len({EXIT_SUCCESS, EXIT_BUSINESS_OUTCOME, EXIT_FAILED, EXIT_POLICY_VIOLATION, EXIT_BLOCKED, EXIT_USAGE}) == 6


@pytest.mark.parametrize(
    "status, bad",
    [
        (ReplayStatus.SUCCESS, dict(failure=FailureDetail(kind=FailureKind.APP_ERROR, observed_summary="x"))),
        (ReplayStatus.FAILED, dict(outputs={"x": 1})),
        (ReplayStatus.BUSINESS_OUTCOME, dict(blocked=BlockedDetail(kind=BlockedKind.APPROVAL_DENIED, step_id="s", lease_state="PAUSED"))),
    ],
)
def test_arms_are_mutually_exclusive(status, bad) -> None:
    base = replay(status, fkind=FailureKind.APP_ERROR, bkind=BlockedKind.APPROVAL_REQUIRED).model_dump()
    base.update({k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in bad.items()})
    with pytest.raises(ValidationError):
        ReplayResult.model_validate(base)


def test_all_status_kind_products_construct_or_reject_cleanly() -> None:
    """No combination crashes: it is either a valid result with a defined exit code, or a
    ValidationError — never an undefined exit."""
    for status, fkind, bkind in product(ReplayStatus, [None, *FailureKind], [None, *BlockedKind]):
        try:
            r = replay(status, fkind=fkind, bkind=bkind)
        except (ValidationError, AssertionError):
            continue
        assert exit_code(r) in {EXIT_SUCCESS, EXIT_BUSINESS_OUTCOME, EXIT_FAILED, EXIT_POLICY_VIOLATION, EXIT_BLOCKED}


def test_discovery_statuses_are_exhaustively_tested() -> None:
    from typing import get_args

    literal = DiscoveryResult.model_fields["status"].annotation
    assert set(get_args(literal)) == {"SUCCESS", "GAVE_UP", "BLOCKED", "FAILED"}
    for b in BlockedKind:
        assert exit_code(discovery("BLOCKED", bkind=b)) == EXIT_BLOCKED
