"""Loader rules that need artifact + profile + policy + overlay together (SPEC §4).

They cannot be pydantic validators because a single model never sees the other three:
(1) closure of the outcome space, (2) forbidden extraction, (3) approval hash,
(4) schema_version major, (5) profile patterns ⊆ policy patterns, plus overlay
narrowing/retargeting. Loader refusals raise ``LoaderError``; the CLI maps that to exit 2
with a one-line message and no run directory (SPEC §5).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import ValidationError

from anton.policy.gate import PolicyConfig, glob_covers, glob_match, load_policy
from anton.schema.app_profile import AppProfile, TenantOverlay
from anton.schema.artifact import (
    SCHEMA_VERSION,
    BBoxLocator,
    BusinessDetector,
    CapabilityArtifact,
    ExactTextLocator,
    LabelRelativeLocator,
    LocatorLadder,
    OutcomeDetector,
    OutputSpec,
    RecoverableDetector,
    RoleNameLocator,
    RunFlow,
    StepSpec,
    content_hash,
)

__all__ = [
    "LoaderError",
    "OverlaySources",
    "LoadedBundle",
    "repo_root",
    "resolve_repo_path",
    "load_artifact",
    "load_profile",
    "load_overlay",
    "normalize_approval",
    "approval_is_valid",
    "check_flows",
    "check_schema_version",
    "check_closure",
    "check_forbidden_extraction",
    "check_profile_subset",
    "apply_overlay",
    "glob_match",
    "glob_covers",
    "reachable_business_codes",
    "load_bundle",
]


class LoaderError(Exception):
    """A refusal with a one-line, human-readable message."""


def repo_root() -> Path:
    """The repository root, located from the package — never the current working directory."""
    return Path(__file__).resolve().parents[2]


def resolve_repo_path(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else repo_root() / path


# --- file loading -------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise LoaderError(f"file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise LoaderError(f"{path}: invalid JSON ({e.msg} at line {e.lineno})") from e


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise LoaderError(f"file not found: {path}") from e
    except yaml.YAMLError as e:
        raise LoaderError(f"{path}: invalid YAML ({e})") from e
    if not isinstance(data, dict):
        raise LoaderError(f"{path}: expected a mapping at top level")
    return data


def _one_line(err: ValidationError) -> str:
    first = err.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", ()))
    return f"{loc}: {first.get('msg', 'invalid')}" if loc else str(first.get("msg", "invalid"))


def load_artifact(path: str | Path) -> CapabilityArtifact:
    """Parse, check ``schema_version`` and normalize the approval block (rules 3 and 4)."""
    p = resolve_repo_path(path)
    try:
        artifact = CapabilityArtifact.model_validate(_read_json(p))
    except ValidationError as e:
        raise LoaderError(f"{p}: {_one_line(e)}") from e
    check_schema_version(artifact)
    return normalize_approval(artifact)


def load_profile(path: str | Path) -> AppProfile:
    p = resolve_repo_path(path)
    try:
        return AppProfile.model_validate(_read_yaml(p))
    except ValidationError as e:
        raise LoaderError(f"{p}: {_one_line(e)}") from e


def load_overlay(path: str | Path) -> TenantOverlay:
    p = resolve_repo_path(path)
    try:
        return TenantOverlay.model_validate(_read_yaml(p))
    except ValidationError as e:
        raise LoaderError(f"{p}: {_one_line(e)}") from e


# --- rules ---------------------------------------------------------------------------------


def check_schema_version(artifact: CapabilityArtifact) -> None:
    have, want = artifact.schema_version.split(".")[0], SCHEMA_VERSION.split(".")[0]
    if have != want:
        raise LoaderError(
            f"schema_version {artifact.schema_version} is incompatible with {SCHEMA_VERSION} (major differs)"
        )


def normalize_approval(artifact: CapabilityArtifact) -> CapabilityArtifact:
    """An approved artifact whose hash mismatches is refused; a draft ignores a stale block."""
    if artifact.status == "approved":
        assert artifact.approval is not None  # guaranteed by the model validator
        expected = content_hash(artifact)
        if artifact.approval.artifact_sha256 != expected:
            raise LoaderError(
                f"{artifact.capability_id}: approval hash mismatch — the artifact was edited after "
                f"approval (approval {artifact.approval.artifact_sha256[:12]}…, content {expected[:12]}…)"
            )
        return artifact
    if artifact.approval is not None:
        return artifact.model_copy(update={"approval": None})
    return artifact


def approval_is_valid(artifact: CapabilityArtifact) -> bool:
    return (
        artifact.status == "approved"
        and artifact.approval is not None
        and artifact.approval.artifact_sha256 == content_hash(artifact)
    )


def reachable_business_codes(
    artifact: CapabilityArtifact, profile: AppProfile, overlay: TenantOverlay | None = None
) -> dict[str, str]:
    """Every BUSINESS outcome_code reachable at replay → the detector id that emits it."""
    codes: dict[str, str] = {}
    detectors: list[OutcomeDetector] = list(profile.global_detectors)
    for s in artifact.steps:
        detectors.extend(s.detectors)
    if overlay is not None:
        for so in overlay.step_overrides.values():
            detectors.extend(so.extra_detectors)
    for d in detectors:
        if isinstance(d, BusinessDetector):
            codes[d.outcome_code] = d.id
    return codes


def check_closure(
    artifact: CapabilityArtifact, profile: AppProfile, overlay: TenantOverlay | None = None
) -> None:
    """Rule 1: the declared outcome space is closed."""
    reachable = reachable_business_codes(artifact, profile, overlay)
    missing = sorted(code for code in reachable if code not in artifact.business_outcomes)
    if missing:
        raise LoaderError(
            f"{artifact.capability_id}: detectors emit outcome codes not declared in "
            f"business_outcomes: {missing} (declared: {sorted(artifact.business_outcomes)})"
        )


def _ladder_strings(ladder: LocatorLadder) -> list[str]:
    out: list[str] = []
    for c in ladder.candidates:
        if isinstance(c, RoleNameLocator):
            out.append(c.name)
        elif isinstance(c, LabelRelativeLocator):
            out.extend(x for x in (c.anchor_text, c.anchor_col_header, c.col_header) if x)
        elif isinstance(c, ExactTextLocator):
            out.append(c.text)
        elif isinstance(c, BBoxLocator):
            pass
    fp = ladder.fingerprint
    out.extend(x for x in (fp.name_snippet, fp.text_snippet) if x)
    return out


def check_forbidden_extraction(artifact: CapabilityArtifact, forbidden_patterns: list[str]) -> None:
    """Rule 2: no output may be extracted from an element whose label context is forbidden."""
    compiled = [re.compile(p) for p in forbidden_patterns]
    for out in artifact.outputs:
        for s in _ladder_strings(out.extraction.locator):
            for rx in compiled:
                if rx.search(s):
                    raise LoaderError(
                        f"{artifact.capability_id}: output {out.name!r} extracts from a forbidden "
                        f"element (locator text {s!r} matches {rx.pattern!r})"
                    )


def _flow_ids_in(detectors: list[OutcomeDetector]) -> set[str]:
    return {d.recovery.flow_id for d in detectors if isinstance(d, RecoverableDetector) and isinstance(d.recovery, RunFlow)}


def check_flows(artifact: CapabilityArtifact, profile: AppProfile) -> None:
    """Every ``RunFlow.flow_id`` reachable at replay and every ``requires_flows`` entry must name
    a flow in ``profile.flows`` — the loader is the only place that sees both."""
    needed: set[str] = set(artifact.requires_flows)
    needed |= _flow_ids_in(profile.global_detectors)
    for s in artifact.steps:
        needed |= _flow_ids_in(s.detectors)
    for steps in profile.flows.values():
        for s in steps:
            needed |= _flow_ids_in(s.detectors)
    unknown = sorted(needed - set(profile.flows))
    if unknown:
        raise LoaderError(
            f"{artifact.capability_id}: flows {unknown} are referenced but not defined in "
            f"profile {profile.app_id!r} (defined: {sorted(profile.flows)})"
        )


def check_profile_subset(effective_patterns: list[str], policy: PolicyConfig) -> None:
    """Rule 5: every (prefixed) profile pattern is covered by some policy pattern."""
    for fp in effective_patterns:
        if not any(glob_covers(pp, fp) for pp in policy.allowed_url_patterns):
            raise LoaderError(
                f"profile url pattern {fp!r} is not covered by the policy allowlist {policy.allowed_url_patterns}"
            )


# --- overlay ------------------------------------------------------------------------------


@dataclass
class OverlaySources:
    """Which parts of the effective artifact came from the overlay (feeds resolution.source)."""

    step_targets: set[str] = field(default_factory=set)
    step_postconditions: set[str] = field(default_factory=set)
    step_timeouts: set[str] = field(default_factory=set)
    step_extra_detectors: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    success: bool = False


def apply_overlay(
    artifact: CapabilityArtifact, overlay: TenantOverlay
) -> tuple[CapabilityArtifact, OverlaySources]:
    """Narrow or retarget only: never add/remove steps, change names/types, lower risk,
    remove a detector, or introduce an outcome code."""
    if overlay.base_capability_id != artifact.capability_id:
        raise LoaderError(
            f"overlay {overlay.overlay_id} targets {overlay.base_capability_id!r}, "
            f"not {artifact.capability_id!r}"
        )
    step_ids = {s.id for s in artifact.steps}
    unknown_steps = sorted(set(overlay.step_overrides) - step_ids)
    if unknown_steps:
        raise LoaderError(f"overlay {overlay.overlay_id} overrides unknown steps {unknown_steps} (overlays cannot add steps)")
    output_names = {o.name for o in artifact.outputs}
    unknown_outputs = sorted(set(overlay.output_overrides) - output_names)
    if unknown_outputs:
        raise LoaderError(f"overlay {overlay.overlay_id} overrides unknown outputs {unknown_outputs}")
    for sid, so in overlay.step_overrides.items():
        for d in so.extra_detectors:
            if isinstance(d, BusinessDetector) and d.outcome_code not in artifact.business_outcomes:
                raise LoaderError(
                    f"overlay {overlay.overlay_id} step {sid!r} introduces outcome code "
                    f"{d.outcome_code!r} not declared in business_outcomes"
                )

    sources = OverlaySources()
    steps: list[StepSpec] = []
    for s in artifact.steps:
        so = overlay.step_overrides.get(s.id)
        if so is None:
            steps.append(s)
            continue
        data = s.model_dump()
        if so.target is not None:
            data["target"] = so.target.model_dump()
            sources.step_targets.add(s.id)
            if so.postcondition is None and s.target is not None:
                # Retargeting a step retargets the predicates that pointed at its old target
                # (a FILL's FieldValue must stay on the field it fills).
                old = s.target.model_dump()
                for pred in data["postcondition"]["all_of"]:
                    if pred.get("kind") in ("field_value", "checked") and pred.get("ladder") == old:
                        pred["ladder"] = so.target.model_dump()
                        sources.step_postconditions.add(s.id)  # decisions.md P1-3
        if so.postcondition is not None:
            data["postcondition"] = so.postcondition.model_dump()
            sources.step_postconditions.add(s.id)
        if so.wait_timeout_ms is not None:
            data["wait_timeout_ms"] = so.wait_timeout_ms
            sources.step_timeouts.add(s.id)
        if so.extra_detectors:
            data["detectors"] = data["detectors"] + [d.model_dump() for d in so.extra_detectors]
            sources.step_extra_detectors.add(s.id)
        try:
            steps.append(StepSpec.model_validate(data))
        except ValidationError as e:
            raise LoaderError(f"overlay {overlay.overlay_id} step {s.id!r}: {_one_line(e)}") from e

    outputs: list[OutputSpec] = []
    for o in artifact.outputs:
        ext = overlay.output_overrides.get(o.name)
        if ext is None:
            outputs.append(o)
        else:
            outputs.append(o.model_copy(update={"extraction": ext}))
            sources.outputs.add(o.name)

    update: dict = {"steps": steps, "outputs": outputs}
    if overlay.success is not None:
        update["success"] = overlay.success
        sources.success = True
    try:
        effective = CapabilityArtifact.model_validate(artifact.model_dump() | {k: [x.model_dump() for x in v] if isinstance(v, list) else v.model_dump() for k, v in update.items()})
    except ValidationError as e:
        raise LoaderError(f"overlay {overlay.overlay_id}: {_one_line(e)}") from e
    return effective, sources


# --- bundle ------------------------------------------------------------------------------


@dataclass
class LoadedBundle:
    """Everything the engine (and ``inspect``) needs, with every loader rule already applied."""

    base: CapabilityArtifact
    artifact: CapabilityArtifact
    profile: AppProfile
    policy: PolicyConfig
    overlay: TenantOverlay | None
    sources: OverlaySources
    route_prefix: str
    entry_url: str
    effective_allowed_url_patterns: list[str]

    @property
    def tenant_id(self) -> str:
        return self.overlay.tenant_id if self.overlay is not None else self.profile.tenant_id


def _compose_entry_url(url: str | None, overlay: TenantOverlay | None, profile: AppProfile) -> str:
    """``--url`` > overlay.entry_url > profile.entry_url; an origin-only ``--url`` takes its
    path from the next layer."""
    layered = overlay.entry_url if overlay is not None and overlay.entry_url else profile.entry_url
    if url is None:
        return layered
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LoaderError(f"--url {url!r} is not an absolute http(s) URL")
    origin_only = parts.path in ("", "/") and not parts.query and not parts.fragment
    if not origin_only:
        if parts.path in ("", "/"):
            raise LoaderError(f"--url {url!r}: an origin-only URL may not carry a query or fragment")
        return url
    lp = urlsplit(layered)
    return urlunsplit((parts.scheme, parts.netloc, lp.path or "/", lp.query, lp.fragment))


def load_bundle(
    artifact_path: str | Path,
    *,
    artifact: CapabilityArtifact | None = None,
    overlay_path: str | Path | None = None,
    policy: PolicyConfig | None = None,
    policy_path: str | Path | None = None,
    url: str | None = None,
) -> LoadedBundle:
    """Load and validate artifact + profile + policy (+ overlay) with every §4 rule applied.

    ``artifact`` lets a caller that already parsed the file (``anton approve``) skip the
    stale-hash refusal while still running every other rule.
    """
    base = artifact if artifact is not None else load_artifact(artifact_path)
    profile = load_profile(base.app_profile)
    try:
        pol = policy if policy is not None else load_policy(policy_path)
    except (OSError, yaml.YAMLError, ValueError) as e:  # pydantic's ValidationError is a ValueError
        raise LoaderError(f"policy: {e}") from e
    overlay = load_overlay(overlay_path) if overlay_path is not None else None
    if overlay is not None and overlay.app_id != profile.app_id:
        raise LoaderError(f"overlay {overlay.overlay_id} is for app {overlay.app_id!r}, profile is {profile.app_id!r}")

    effective, sources = apply_overlay(base, overlay) if overlay is not None else (base, OverlaySources())
    # The overlay's extra detectors are already merged into `effective`; do not count them twice.
    check_closure(effective, profile, None)
    check_forbidden_extraction(effective, pol.forbidden_extract_patterns)
    check_flows(effective, profile)

    route_prefix = overlay.route_prefix if overlay is not None and overlay.route_prefix is not None else profile.route_prefix
    effective_patterns = [route_prefix + p for p in profile.allowed_url_patterns]
    check_profile_subset(effective_patterns, pol)
    entry_url = _compose_entry_url(url, overlay, profile)
    return LoadedBundle(
        base=base,
        artifact=effective,
        profile=profile,
        policy=pol,
        overlay=overlay,
        sources=sources,
        route_prefix=route_prefix,
        entry_url=entry_url,
        effective_allowed_url_patterns=effective_patterns,
    )
