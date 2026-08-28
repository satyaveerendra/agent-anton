"""Result contract (SPEC §5).

Four terminal statuses. RECOVERABLE is internal only and never a terminal status.
``BLOCKED`` means "a human must act; the run is not broken" — filing that under FAILED
would be the glossary's outcome/failure conflation one level up.

The exit code is a **pure function** of the result (``exit_code``); nothing else may decide it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from typing import Literal

from pydantic import Field, model_validator

from anton.schema.artifact import (
    RESERVED_STEP_IDS,
    ElementFingerprint,
    LocatorTier,
    StateSignature,
    StrictModel,
)

__all__ = [
    "RESERVED_STEP_IDS",
    "ReplayStatus",
    "FailureKind",
    "BlockedKind",
    "StepStatus",
    "LocatorResolution",
    "StepResult",
    "InterventionRequest",
    "InterventionRecord",
    "FailureDetail",
    "BlockedDetail",
    "ReplayResult",
    "DiscoveryResult",
    "exit_code",
]


class ReplayStatus(StrEnum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class FailureKind(StrEnum):
    CHECKPOINT_MISS = "CHECKPOINT_MISS"
    LOCATOR_EXHAUSTED = "LOCATOR_EXHAUSTED"
    APP_ERROR = "APP_ERROR"
    POLICY_VIOLATION = "POLICY_VIOLATION"


class BlockedKind(StrEnum):
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    ESCALATION_UNRESOLVED = "ESCALATION_UNRESOLVED"


class StepStatus(StrEnum):
    PASSED = "PASSED"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    #: Only for declared steps that ``resume_point()`` jumped over.
    SKIPPED = "SKIPPED"
    HUMAN_COMPLETED = "HUMAN_COMPLETED"


class LocatorResolution(StrictModel):
    tier: LocatorTier
    candidate_index: int = Field(ge=0)
    source: Literal["artifact", "overlay"]
    fingerprint_ok: bool


class StepResult(StrictModel):
    #: A StepSpec id or a reserved pseudo-step id.
    step_id: str
    status: StepStatus
    attempts: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    #: None for steps without a target.
    resolution: LocatorResolution | None = None
    #: True if only tier 3 (visual) resolved — early-warning signal.
    drift_flag: bool = False
    #: Detector ids that fired and were handled (incl. inside flows run for this step).
    recoveries: list[str] = Field(default_factory=list)
    #: Every detector that matched at this step, handled or terminal.
    detectors_fired: list[str] = Field(default_factory=list)
    #: Relative paths (screenshots, snapshots) captured at this step.
    evidence: list[str] = Field(default_factory=list)


InterventionKind = Literal["TAKEOVER", "APPROVAL"]


class InterventionRequest(StrictModel):
    """Written to ``runs/<id>/intervention.json`` on trigger (SPEC §11)."""

    request_id: str
    run_id: str
    kind: InterventionKind
    capability_id: str | None = None
    goal: str | None = None
    step_id: str
    reason: str
    #: Masked, relative path.
    screenshot: str
    page_signature: str
    target_fingerprint: ElementFingerprint | None = None
    target_rationale: str | None = None
    expected: StateSignature | None = None
    console_url: str
    requested_at: datetime
    #: The post-transition value (PAUSED).
    lease_state: str


class InterventionRecord(StrictModel):
    request_id: str
    kind: InterventionKind
    at_step: str
    reason: str
    requested_at: datetime
    control_taken_at: datetime | None = None
    handed_back_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: Literal["resumed", "approved", "denied", "abandoned", "timed_out", "resume_failed"]
    human_action_count: int = Field(ge=0)
    human_actions_path: str | None = None
    #: "__outputs__" when the furthest satisfied signature was ``success``.
    resumed_at_step: str | None = None


class FailureDetail(StrictModel):
    kind: FailureKind
    step_id: str | None = None
    #: None for POLICY_VIOLATION (nothing was being checked).
    expected: StateSignature | None = None
    observed_summary: str
    #: Masked screenshot + redacted page snapshot; may be empty for POLICY_VIOLATION.
    evidence: list[str] = Field(default_factory=list)


class BlockedDetail(StrictModel):
    kind: BlockedKind
    step_id: str
    intervention_request_path: str | None = None
    lease_state: str


class ReplayResult(StrictModel):
    run_id: str
    capability_id: str
    version: str
    tenant_id: str | None = None
    overlay_id: str | None = None
    artifact_sha256: str
    status: ReplayStatus
    #: SUCCESS only; coerced per OutputSpec.type.
    outputs: dict[str, str | int | float | bool] | None = None
    #: BUSINESS_OUTCOME only.
    outcome_code: str | None = None
    outcome_step_id: str | None = None
    outcome_detector_id: str | None = None
    #: The observed message, redacted.
    outcome_detail: str | None = None
    #: FAILED only.
    failure: FailureDetail | None = None
    #: BLOCKED only.
    blocked: BlockedDetail | None = None
    steps: list[StepResult] = Field(default_factory=list)
    interventions: list[InterventionRecord] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
    #: True on stdout and in result.json; only --outputs-file carries raw sensitive outputs.
    redacted_view: bool

    @model_validator(mode="after")
    def _arms_are_exclusive(self) -> ReplayResult:
        s = self.status
        if (self.outputs is not None) != (s is ReplayStatus.SUCCESS):
            raise ValueError("outputs are present iff status == SUCCESS")
        business = s is ReplayStatus.BUSINESS_OUTCOME
        if (self.outcome_code is not None) != business or (self.outcome_step_id is not None) != business:
            raise ValueError("outcome_code/outcome_step_id are present iff status == BUSINESS_OUTCOME")
        if (self.outcome_detector_id is not None) != business or (self.outcome_detail is not None and not business):
            raise ValueError("outcome_detector_id/outcome_detail are present only on BUSINESS_OUTCOME")
        if (self.failure is not None) != (s is ReplayStatus.FAILED):
            raise ValueError("failure is present iff status == FAILED")
        if (self.blocked is not None) != (s is ReplayStatus.BLOCKED):
            raise ValueError("blocked is present iff status == BLOCKED")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self


class DiscoveryResult(StrictModel):
    """Written by ``anton discover``."""

    run_id: str
    goal: str
    status: Literal["SUCCESS", "GAVE_UP", "BLOCKED", "FAILED"]
    #: FAILED only (POLICY_VIOLATION ⇒ exit 30).
    failure_kind: FailureKind | None = None
    #: BLOCKED only.
    blocked: BlockedDetail | None = None
    artifact_path: str | None = None
    turns: int = Field(ge=0)
    reason: str | None = None
    interventions: list[InterventionRecord] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def _arms_are_exclusive(self) -> DiscoveryResult:
        if (self.failure_kind is not None) != (self.status == "FAILED"):
            raise ValueError("failure_kind is present iff status == FAILED")
        if (self.blocked is not None) != (self.status == "BLOCKED"):
            raise ValueError("blocked is present iff status == BLOCKED")
        if (self.artifact_path is not None) != (self.status == "SUCCESS"):
            raise ValueError("artifact_path is present iff status == SUCCESS")
        return self


EXIT_SUCCESS = 0
EXIT_BUSINESS_OUTCOME = 10
EXIT_FAILED = 20
EXIT_POLICY_VIOLATION = 30
EXIT_BLOCKED = 40
#: Invalid ``--param`` values and loader refusals: no result exists yet (SPEC §5).
EXIT_USAGE = 2


def exit_code(result: ReplayResult | DiscoveryResult) -> int:
    """The CLI exit code as a pure function of the result (SPEC §5).

    0 SUCCESS · 10 BUSINESS_OUTCOME · 20 FAILED (or GAVE_UP) with kind != POLICY_VIOLATION
    · 30 FAILED with kind == POLICY_VIOLATION · 40 BLOCKED.
    """
    status = result.status.value if isinstance(result.status, Enum) else result.status
    if status == "SUCCESS":
        return EXIT_SUCCESS
    if status == "BUSINESS_OUTCOME":
        return EXIT_BUSINESS_OUTCOME
    if status == "BLOCKED":
        return EXIT_BLOCKED
    if status == "GAVE_UP":
        return EXIT_FAILED
    # FAILED — the validators guarantee the detail is present.
    if isinstance(result, ReplayResult):
        kind = result.failure.kind if result.failure is not None else None
    else:
        kind = result.failure_kind
    if kind is FailureKind.POLICY_VIOLATION:
        return EXIT_POLICY_VIOLATION
    return EXIT_FAILED
