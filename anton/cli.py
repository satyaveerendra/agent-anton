"""``python -m anton <cmd>`` — a thin wrapper (SPEC §13).

Phase 1 ships ``inspect`` and ``approve``. ``target``, ``discover`` and ``replay`` are
registered so the usage text is complete, and exit 2 with a clear message until their
phase lands (SPEC §17).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from anton.policy.gate import PolicyConfig
from anton.schema.artifact import (
    Approval,
    BBoxLocator,
    CapabilityArtifact,
    ExactTextLocator,
    LabelRelativeLocator,
    LocatorLadder,
    RiskClass,
    RoleNameLocator,
    StepSpec,
    content_hash,
)
from anton.schema.loader import (
    LoadedBundle,
    LoaderError,
    approval_is_valid,
    check_schema_version,
    load_bundle,
    reachable_business_codes,
    resolve_repo_path,
)
from anton.schema.result import EXIT_USAGE

NOT_YET = {
    "target": "phase 2",
    "discover": "phase 5",
    "replay": "phase 4",
}


def _candidate_summary(ladder: LocatorLadder) -> list[str]:
    lines: list[str] = []
    for i, c in enumerate(ladder.candidates):
        frame = f" in {'/'.join(c.frame_path)}" if c.frame_path else ""
        if isinstance(c, RoleNameLocator):
            what = f"role_name {c.role} {c.name!r}{' (exact)' if c.exact else ''}"
        elif isinstance(c, LabelRelativeLocator):
            extra = f" col={c.col_header!r}" if c.col_header else ""
            anchor_col = f" anchor_col={c.anchor_col_header!r}" if c.anchor_col_header else ""
            what = f"{c.relation} {c.anchor_text!r}{anchor_col}{extra}"
        elif isinstance(c, ExactTextLocator):
            what = f"exact_text {c.text!r}"
        elif isinstance(c, BBoxLocator):
            what = f"bbox ({c.x:.3f},{c.y:.3f},{c.w:.3f},{c.h:.3f})"
        else:  # pragma: no cover - exhaustive by union
            what = c.kind
        lines.append(f"      [{i}] tier {int(c.tier)} {what}{frame} — {c.rationale}")
    fp = ladder.fingerprint
    lines.append(
        f"      fingerprint: role={fp.role!r} control_type={fp.control_type!r} "
        f"name~={fp.name_snippet!r} text~={fp.text_snippet!r}"
    )
    return lines


def render_inspect(bundle: LoadedBundle, out: TextIO) -> None:
    a = bundle.artifact
    valid = approval_is_valid(bundle.base)
    w = out.write
    w(f"capability   {a.capability_id}  v{a.version}  schema {a.schema_version}\n")
    w(f"name         {a.name}\n")
    w(f"description  {a.description}\n")
    w(f"status       {a.status}")
    if a.status == "approved" and valid:
        assert a.approval is not None
        # A mismatched hash never reaches here: the loader refuses it (SPEC §4 rule 3).
        w(f"  (approved by {a.approval.approved_by} at {a.approval.approved_at.isoformat()}; hash VALID)")
    w("\n")
    w(f"content hash {content_hash(bundle.base)}\n")
    w(f"profile      {a.app_profile}  app={bundle.profile.app_id} tenant={bundle.tenant_id}")
    if bundle.overlay is not None:
        w(f"  overlay={bundle.overlay.overlay_id}")
    w("\n")
    w(f"entry url    {bundle.entry_url}   route_prefix={bundle.route_prefix or '(none)'}\n")
    w(f"provenance   {a.provenance.origin}  run={a.provenance.discovery_run_id or '-'}  model={a.provenance.model or '-'}  "
      f"recorded against {a.provenance.recorded_against.app_id}/{a.provenance.recorded_against.tenant_id}\n")

    w("\nINPUTS\n")
    for p in a.inputs:
        w(f"  {p.name:<18} {p.type.value:<8} {'required' if p.required else 'optional':<9} "
          f"{'SENSITIVE ' if p.sensitive else ''}pattern={p.pattern or '-'}  {p.description}\n")
    w("\nOUTPUTS\n")
    for o in a.outputs:
        src = "overlay" if o.name in bundle.sources.outputs else "artifact"
        w(f"  {o.name:<18} {o.type.value:<8} {'SENSITIVE ' if o.sensitive else ''}attr={o.extraction.attr} "
          f"normalize={o.extraction.normalize} source={src}  {o.description}\n")
        for line in _candidate_summary(o.extraction.locator):
            w(line + "\n")

    w("\nPRECONDITIONS\n")
    w(f"  requires_flows: {', '.join(a.requires_flows)}\n")
    for p in a.preconditions.all_of:
        w(f"  {p.model_dump_json()}\n")

    w("\nOUTCOME SPACE (effective, closed)\n")
    reachable = reachable_business_codes(a, bundle.profile, bundle.overlay)
    for code, meaning in a.business_outcomes.items():
        src = "app" if any(d.id == reachable.get(code) for d in bundle.profile.global_detectors) else "capability"
        w(f"  {code:<22} via {reachable.get(code, '-'):<22} [{src}]  {meaning}\n")

    w("\nSTEPS\n")
    for s in a.steps:
        risk = _effective_risk_label(s, bundle.policy)
        src = "overlay" if s.id in bundle.sources.step_targets else "artifact"
        val = f" value={s.value.model_dump_json()}" if s.value is not None else ""
        w(f"  {s.id}: {s.action.value}{val}  {risk}  timeout={s.wait_timeout_ms}ms"
          f"{' (overlay)' if s.id in bundle.sources.step_timeouts else ''} retries={s.retries}  — {s.description}\n")
        if s.target is not None:
            w(f"    target ({src}):\n")
            for line in _candidate_summary(s.target):
                w(line + "\n")
        post_src = " (overlay, retargeted)" if s.id in bundle.sources.step_postconditions else ""
        w(f"    postcondition{post_src}: {[p.kind for p in s.postcondition.all_of]}\n")
        if s.detectors:
            det_src = " (incl. overlay extra_detectors)" if s.id in bundle.sources.step_extra_detectors else ""
            w(f"    detectors{det_src}: {[d.id for d in s.detectors]}\n")

    w(f"\nSUCCESS{' (overlay)' if bundle.sources.success else ''}\n")
    for p in a.success.all_of:
        w(f"  {p.model_dump_json()}\n")


def _effective_risk_label(step: StepSpec, policy: PolicyConfig) -> str:
    """Effective risk = max(policy match at act-time, declared). The URL half of a rule is only
    knowable at act-time, so this is a static lower bound with the caveat spelled out."""
    if step.risk is RiskClass.RISKY:
        return "RISKY (declared; approval required)"
    tier1_name = None
    if step.target is not None:
        first = step.target.candidates[0]
        if isinstance(first, RoleNameLocator):
            tier1_name = first.name
    for i, rule in enumerate(policy.risky_rules, start=1):
        if step.action not in rule.actions:
            continue
        name_ok = rule.name_matches is None or (tier1_name is not None and re.search(rule.name_matches, tier1_name) is not None)
        if name_ok:
            when = "url checked at act-time" if rule.url_matches else "static match"
            return f"RISKY by policy rule {i} ({when}; approval required)"
    return "safe (declared; policy url rules resolved at act-time)"


def cmd_inspect(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.artifact, overlay_path=args.overlay, policy_path=args.policy)
    render_inspect(bundle, sys.stdout)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    path = resolve_repo_path(args.artifact)
    # `approve` is the human act that (re)binds the hash, so it parses the file WITHOUT the
    # loader's stale-hash refusal (decisions.md P1-13); everything else still must load.
    try:
        artifact = CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise LoaderError(f"file not found: {path}") from e
    except ValueError as e:  # JSON or ValidationError
        raise LoaderError(f"{path}: {e.errors()[0]['msg'] if isinstance(e, ValidationError) else e}") from e
    check_schema_version(artifact)
    digest = content_hash(artifact)
    if artifact.status == "approved" and artifact.approval is not None:
        if artifact.approval.artifact_sha256 == digest:
            print(f"{artifact.capability_id}: already approved (hash {digest[:12]}…)")
            return 0
        print(f"{artifact.capability_id}: approval is stale (edited after approval) — re-approving")
    # The whole contract must load with its profile and policy before it can be approved;
    # the draft-normalized form is checked so a stale block cannot block re-approval.
    neutral = artifact.model_copy(update={"status": "draft", "approval": None})
    load_bundle(path, artifact=neutral, policy_path=args.policy)
    approved = artifact.model_copy(
        update={
            "status": "approved",
            "approval": Approval(
                approved_at=datetime.now(UTC),
                approved_by=args.by or os.environ.get("USER", "unknown"),
                artifact_sha256=digest,
            ),
        }
    )
    # Re-validate the final object, then write it back with a stable layout.
    approved = CapabilityArtifact.model_validate(approved.model_dump(mode="json"))
    path.write_text(approved.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"{artifact.capability_id}: approved by {approved.approval.approved_by if approved.approval else '?'} (hash {digest[:12]}…) → {path}")
    return 0


def cmd_not_yet(args: argparse.Namespace) -> int:
    print(f"anton {args.command}: not available until {NOT_YET[args.command]} (SPEC §17)", file=sys.stderr)
    return EXIT_USAGE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anton", description="Digital back-office operator: learns a flow once, replays it exactly.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="render a capability artifact as a reviewable contract")
    p_inspect.add_argument("artifact", type=Path)
    p_inspect.add_argument("--overlay", type=Path, default=None)
    p_inspect.add_argument("--policy", type=Path, default=None)
    p_inspect.set_defaults(func=cmd_inspect)

    p_approve = sub.add_parser("approve", help="flip draft → approved, binding the approval to a content hash")
    p_approve.add_argument("artifact", type=Path)
    p_approve.add_argument("--by", default=None)
    p_approve.add_argument("--policy", type=Path, default=None)
    p_approve.set_defaults(func=cmd_approve)

    for name in NOT_YET:
        p = sub.add_parser(name, help=f"(available from {NOT_YET[name]})")
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(func=cmd_not_yet)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in NOT_YET:
        print(f"anton {raw[0]}: not available until {NOT_YET[raw[0]]} (SPEC §17)", file=sys.stderr)
        return EXIT_USAGE
    args = build_parser().parse_args(raw)
    try:
        return int(args.func(args))
    except LoaderError as e:
        print(f"anton {args.command}: {e}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as e:  # e.g. the approve write failed — never a traceback, never exit 1
        print(f"anton {args.command}: {e}", file=sys.stderr)
        return EXIT_USAGE
