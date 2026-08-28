"""AppProfile, MaskSpec, TenantOverlay, StepOverride (SPEC §4).

The profile is per-app, human-curated configuration: entry URL, allowed routes, named flows
(``login`` is mandatory), ALL app-level detectors of every arm (the outcome space is declared
per app because one happy-path discovery cannot observe outcomes it never hit), and the
``sensitive_elements`` masks used both for screenshots and for scrubbing snapshot text.

An overlay may only *narrow or retarget* a base artifact for one tenant; the loader
(``anton.schema.loader``) enforces the rules that need artifact + overlay together.
"""

from __future__ import annotations

import re

from pydantic import Field, model_validator

from anton.schema.artifact import (
    ActionType,
    ElementVisible,
    ExtractionSpec,
    LocatorLadder,
    OutcomeDetector,
    StateSignature,
    StepSpec,
    StrictModel,
    UrlMatches,
)

__all__ = ["MaskSpec", "AppProfile", "StepOverride", "TenantOverlay", "LOGIN_FLOW"]

LOGIN_FLOW = "login"


class MaskSpec(StrictModel):
    id: str
    ladder: LocatorLadder
    #: URL regex (path, prefix-stripped).
    applies_to: str
    #: Unresolved required mask ⇒ screenshot fails CLOSED (SPEC §7, §8).
    required: bool = True

    @model_validator(mode="after")
    def _regex_compiles(self) -> MaskSpec:
        re.compile(self.applies_to)
        return self


class AppProfile(StrictModel):
    app_id: str
    #: e.g. "alpha"; ReplayResult.tenant_id = the overlay's if present, else this.
    tenant_id: str
    app_version: str | None = None
    entry_url: str
    route_prefix: str = ""
    #: Must be a subset of the policy's; the effective allowlist prefixes them with route_prefix.
    allowed_url_patterns: list[str] = Field(min_length=1)
    #: Must include "login".
    flows: dict[str, list[StepSpec]]
    #: ALL app-level detectors of every arm.
    global_detectors: list[OutcomeDetector] = Field(default_factory=list)
    #: Single list used for screenshot masking AND snapshot text scrubbing.
    sensitive_elements: list[MaskSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> AppProfile:
        if self.route_prefix and not self.route_prefix.startswith("/"):
            raise ValueError("route_prefix must start with '/'")
        if self.route_prefix.endswith("/"):
            raise ValueError("route_prefix must not end with '/'")
        login = self.flows.get(LOGIN_FLOW)
        if not login:
            raise ValueError("flows must include a non-empty 'login' flow")
        if any(s.action is ActionType.NAVIGATE for s in login):
            raise ValueError("the login flow must not contain a NAVIGATE step (it acts on the page the app redirected to, so ?next= is honoured)")
        last = login[-1].postcondition
        if any(isinstance(p, UrlMatches) for p in last.all_of) or not any(
            isinstance(p, ElementVisible) for p in last.all_of
        ):
            raise ValueError("the login flow's final postcondition must be destination-agnostic: ElementVisible of the authenticated landmark, no UrlMatches")
        for flow_id, steps in self.flows.items():
            ids = [s.id for s in steps]
            if len(set(ids)) != len(ids):
                raise ValueError(f"flow {flow_id!r}: step ids must be unique")
        det_ids = [d.id for d in self.global_detectors]
        if len(set(det_ids)) != len(det_ids):
            raise ValueError("global_detectors ids must be unique")
        mask_ids = [m.id for m in self.sensitive_elements]
        if len(set(mask_ids)) != len(mask_ids):
            raise ValueError("sensitive_elements ids must be unique")
        return self

    def business_codes(self) -> set[str]:
        return {d.outcome_code for d in self.global_detectors if d.arm == "BUSINESS_OUTCOME"}


class StepOverride(StrictModel):
    """The ONLY fields an overlay may touch on a step."""

    target: LocatorLadder | None = None
    postcondition: StateSignature | None = None
    wait_timeout_ms: int | None = Field(default=None, ge=250)
    #: Business codes must already be keys of the artifact's business_outcomes.
    extra_detectors: list[OutcomeDetector] = Field(default_factory=list)


class TenantOverlay(StrictModel):
    overlay_id: str
    tenant_id: str
    app_id: str
    base_capability_id: str
    entry_url: str | None = None
    #: e.g. "/portal".
    route_prefix: str | None = None
    #: Keyed by step id.
    step_overrides: dict[str, StepOverride] = Field(default_factory=dict)
    #: Keyed by output name.
    output_overrides: dict[str, ExtractionSpec] = Field(default_factory=dict)
    success: StateSignature | None = None

    @model_validator(mode="after")
    def _rules(self) -> TenantOverlay:
        if self.route_prefix is not None:
            if self.route_prefix and not self.route_prefix.startswith("/"):
                raise ValueError("route_prefix must start with '/'")
            if self.route_prefix.endswith("/"):
                raise ValueError("route_prefix must not end with '/'")
        return self
