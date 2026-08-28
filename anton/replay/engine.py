"""Replay engine (SPEC §9) — deterministic, no LLM imports anywhere in ``anton.replay``.

Fixed step order, declarative waits (the only sleep is the 250 ms tick), bounded retries, no
branching beyond declared detectors, ONE resume path (``locate.resume_point``), zero model calls.
Every action passes ``ActionGate.check`` first; every byte written goes through the Redactor.

Terminal outcomes are raised as ``_Terminal`` subclasses from wherever they are decided and
assembled into a ``ReplayResult`` in ``run_replay`` — so the status/exit mapping lives in one place.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anton.evidence.log import RunLog
from anton.policy.gate import (
    ActionGate,
    Allow,
    Deny,
    DenyReason,
    GateContext,
    LeaseState,
    NeedsApproval,
    resolve_secret,
)
from anton.policy.redact import Redactor
from anton.replay.locate import WalkResult, resolve_ladder_until, resume_point, walk_ladder
from anton.replay.outcomes import first_hit, recoverable
from anton.replay.predicates import Evaluator
from anton.schema.artifact import (
    ActionType,
    BusinessDetector,
    CapabilityArtifact,
    Dismiss,
    Escalate,
    HardFailureDetector,
    LiteralValue,
    OutcomeDetector,
    OutputSpec,
    OutputType,
    ParamRef,
    ParamSpec,
    ParamType,
    RecoverableDetector,
    RetryStep,
    RiskClass,
    RunFlow,
    SecretRef,
    StateSignature,
    StepSpec,
    content_hash,
    referenced_param_names,
)
from anton.schema.loader import LoadedBundle, repo_root
from anton.schema.result import (
    BlockedDetail,
    BlockedKind,
    FailureDetail,
    FailureKind,
    InterventionRecord,
    InterventionRequest,
    ReplayResult,
    ReplayStatus,
    StepResult,
    StepStatus,
)
from anton.schema.snapshot import SemanticSnapshot
from anton.surface.base import ActError, ConcreteAction, Surface

__all__ = [
    "PRECONDITIONS",
    "SUCCESS",
    "OUTPUTS",
    "TICK_S",
    "ParamError",
    "validate_params",
    "ReplayOptions",
    "ReplayEngine",
    "run_replay",
    "check_outputs_path",
    "write_outputs_file",
    "new_run_id",
    "coerce_output",
    "normalize_output",
]

PRECONDITIONS, SUCCESS, OUTPUTS = "__preconditions__", "__success__", "__outputs__"
TICK_S = 0.25
DEFAULT_VIEWPORT = (1280, 900)


# --- params ----------------------------------------------------------------------------------------


class ParamError(Exception):
    """Invalid ``--param`` input: exit 2, no run directory (SPEC §5, §9)."""


_BOOL = {"true", "false", "1", "0", "yes", "no"}


def validate_params(artifact: CapabilityArtifact | list[ParamSpec], given: Mapping[str, str]) -> dict[str, str]:
    """Check the caller's strings against ``ParamSpec`` (required, pattern, enum, type). A bare list of
    ``ParamSpec``s (the ``discover --spec`` inputs) skips the artifact-reference check."""
    inputs = artifact if isinstance(artifact, list) else artifact.inputs
    specs = {p.name: p for p in inputs}
    unknown = sorted(set(given) - set(specs))
    if unknown:
        raise ParamError(f"unknown param(s) {unknown}; declared inputs: {sorted(specs)}")
    out: dict[str, str] = {}
    for name, spec in specs.items():
        if name not in given:
            if spec.required:
                raise ParamError(f"missing required param {name!r} ({spec.description})")
            continue
        v = given[name]
        shown = "***" if spec.sensitive else repr(v)  # a sensitive value never reaches stderr
        if spec.pattern is not None and re.search(spec.pattern, v) is None:
            raise ParamError(f"param {name}={shown} does not match pattern {spec.pattern!r}")
        if spec.type is ParamType.INTEGER and re.fullmatch(r"[+-]?\d+", v) is None:
            raise ParamError(f"param {name}={shown} is not an INTEGER")
        if spec.type is ParamType.NUMBER:
            try:
                float(v)
            except ValueError as e:
                raise ParamError(f"param {name}={shown} is not a NUMBER") from e
        if spec.type is ParamType.BOOLEAN and v.lower() not in _BOOL:
            raise ParamError(f"param {name}={shown} is not a BOOLEAN")
        if spec.type is ParamType.ENUM and v not in (spec.enum_values or []):
            raise ParamError(f"param {name}={shown} is not one of {spec.enum_values}")
        out[name] = v
    if isinstance(artifact, list):
        return out
    referenced = referenced_param_names(artifact.model_dump(mode="json", exclude={"inputs"}))
    unsupplied = sorted(referenced - set(out))
    if unsupplied:
        raise ParamError(f"param(s) {unsupplied} are referenced by the artifact's steps but were not supplied")
    return out


# --- outputs ----------------------------------------------------------------------------------------


def normalize_output(raw: str, mode: str) -> str:
    if mode == "none":
        return raw
    if mode == "trim":
        return raw.strip()
    if mode == "money":
        return re.sub(r"[$,\s]", "", raw)
    if mode == "digits":
        return re.sub(r"\D", "", raw)
    raise ValueError(f"unknown normalize mode {mode!r}")  # pragma: no cover - enum-guarded


def coerce_output(spec: OutputSpec, raw: str) -> str | int | float | bool:
    """``post_process_regex`` → ``normalize`` → coerce to ``OutputSpec.type``; ValueError on failure."""
    text = raw
    if spec.extraction.post_process_regex is not None:
        m = re.search(spec.extraction.post_process_regex, text)
        if m is None:
            raise ValueError(f"post_process_regex {spec.extraction.post_process_regex!r} did not match")
        text = m.group(1) if m.groups() else m.group(0)
    text = normalize_output(text, spec.extraction.normalize)
    t = spec.type
    if t is OutputType.STRING:
        return text
    if t is OutputType.INTEGER:
        return int(text)
    if t is OutputType.NUMBER:
        return float(text)
    if t is OutputType.BOOLEAN:
        if text.lower() not in _BOOL:
            raise ValueError(f"{text!r} is not a BOOLEAN")
        return text.lower() in ("true", "1", "yes")
    if text not in (spec.enum_values or []):
        raise ValueError(f"{text!r} is not one of {spec.enum_values}")
    return text


# --- options / ids ----------------------------------------------------------------------------------


@dataclass
class ReplayOptions:
    approve_risky: bool = False
    allow_draft: bool = False
    escalate: bool = False
    escalate_timeout_s: int = 600
    console_port: int = 7071
    outputs_file: Path | None = None
    record_video: bool = False
    #: Explicit run directory (default ``runs/<run_id>`` under ``runs_root``).
    run_dir: Path | None = None
    runs_root: Path | None = None
    #: None → headed unless ``ANTON_HEADLESS=1`` (SPEC §2: headed by default).
    headless: bool | None = None
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    sleep: Callable[[float], None] = time.sleep


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


# --- terminal signals ------------------------------------------------------------------------------


class _Terminal(Exception):
    pass


class _Failed(_Terminal):
    def __init__(self, kind: FailureKind, step_id: str | None, expected: StateSignature | None, observed: str, evidence: list[str] | None = None, deny_reason: DenyReason | None = None, escalated: bool = False) -> None:
        super().__init__(f"{kind}: {observed}")
        self.kind, self.step_id, self.expected, self.observed, self.evidence = kind, step_id, expected, observed, list(evidence or [])
        self.deny_reason = deny_reason
        #: True when escalation already ran for this failure (never escalate twice).
        self.escalated = escalated


class _Business(_Terminal):
    def __init__(self, detector: BusinessDetector, step_id: str, detail: str | None) -> None:
        super().__init__(f"{detector.outcome_code} at {step_id}")
        self.detector, self.step_id, self.detail = detector, step_id, detail


class _Blocked(_Terminal):
    def __init__(self, kind: BlockedKind, step_id: str, intervention: bool = False) -> None:
        super().__init__(f"{kind} at {step_id}")
        self.kind, self.step_id, self.intervention = kind, step_id, intervention


@dataclass
class _StepOutcome:
    kind: str  # "passed" | "jump" | "checkpoint" | "locator"
    resume_to: int | None = None
    observed: str = ""
    expected: StateSignature | None = None
    evidence: list[str] = field(default_factory=list)


# --- the engine ---------------------------------------------------------------------------------------


class ReplayEngine:
    """Drives one run over an already-launched ``Surface``; raises ``_Terminal`` for every
    non-SUCCESS ending and returns the coerced outputs on SUCCESS."""

    def __init__(
        self,
        bundle: LoadedBundle,
        params: Mapping[str, str],
        opts: ReplayOptions,
        surface: Surface,
        gate: ActionGate,
        log: RunLog,
        redactor: Redactor,
        steps_out: list[StepResult],
        escalation: Any | None = None,
    ) -> None:
        self.bundle = bundle
        #: ``EscalationController`` when ``--escalate`` (SPEC §11); None ⇒ every trigger is terminal (§9 default mode).
        self.escalation = escalation
        self.interventions: list[InterventionRecord] = []
        self.artifact = bundle.artifact
        self.profile = bundle.profile
        self.params = dict(params)
        self.opts = opts
        self.surface = surface
        self.gate = gate
        self.log = log
        self.redactor = redactor
        self.steps_out = steps_out
        self.ev = Evaluator(surface, self.params)
        self._secret_values: set[str] = set()
        self._recovery_uses: dict[tuple[str, str], int] = {}
        self._last_logged_url: str | None = None
        self._last_logged_snapshot: SemanticSnapshot | None = None
        self._last_logged_empty = False
        #: The last URL that passed the allowlist; every tick re-checks against it (SPEC §8 layer 2).
        self._checked_url: str | None = None
        self._last_flow_failure = ""
        self._action_seq = 0

    @property
    def lease_state(self) -> LeaseState:
        return self.escalation.lease.state if self.escalation is not None else LeaseState.AGENT_ACTIVE

    # --- observation / logging -------------------------------------------------------------------------

    def _observe(self, step_id: str, *, force: bool = False) -> SemanticSnapshot:
        """Observe; log an ``observation`` on every URL change (and when forced)."""
        snap = self.surface.observe()
        self._log_snapshot(step_id, snap, force=force)
        return snap

    def _log_snapshot(self, step_id: str, snap: SemanticSnapshot, *, force: bool = False) -> None:
        if snap is self._last_logged_snapshot:
            return
        # A URL change is logged with its first NON-EMPTY snapshot too (the first tick after a
        # navigation may see the empty "pending navigation" snapshot).
        refresh = self._last_logged_empty and snap.frame_count > 0 and snap.url == self._last_logged_url
        if force or snap.url != self._last_logged_url or refresh:
            self.log.observation(step_id, snap)
            self._last_logged_url, self._last_logged_snapshot = snap.url, snap
            self._last_logged_empty = snap.frame_count == 0

    def _check_navigation(self, record: StepResult, expected: StateSignature | None, step_id: str | None = None) -> None:
        """Layer 2 of SPEC §8 on EVERY tick: a URL that changed since the last check and is off the
        allowlist stops the run before the page is observed or screenshotted."""
        url = self.surface.url()
        if url == self._checked_url:
            return
        deny = self.gate.post_navigation_check(url, self.bundle.effective_allowed_url_patterns)
        if deny is not None:
            self.log.event("gate", {"step_id": step_id or record.step_id, "decision": "Deny", "reason": deny.reason.value, "detail": deny.detail, "lease_state": self.lease_state.value, "owner": "agent", "phase": "post-navigation"})
            self._deny(deny, record.step_id, expected, record)
        self._checked_url = url

    def _evidence(self, sr: StepResult, snap: SemanticSnapshot | None = None) -> list[str]:
        """Masked screenshot + redacted page snapshot, recorded on the step (SPEC §9, §12)."""
        paths: list[str] = []
        try:
            shot = self.surface.screenshot(self.profile.sensitive_elements)
            paths.append(self.log.relative(shot))
        except Exception as e:  # noqa: BLE001 - evidence capture must never mask the real failure
            self.log.event("evidence_error", {"step_id": sr.step_id, "detail": str(e)[:200]})
        try:
            page = snap if snap is not None else self.surface.observe()
            n = sum(1 for p in sr.evidence if p.endswith(".json")) + 1
            out = self.log.write_json(f"snapshot-{sr.step_id}-{n}.json", page)
            paths.append(self.log.relative(out))
        except Exception as e:  # noqa: BLE001
            self.log.event("evidence_error", {"step_id": sr.step_id, "detail": str(e)[:200]})
        sr.evidence.extend(paths)
        return paths

    def _summary(self, snap: SemanticSnapshot) -> str:
        texts = [e.text for e in snap.elements if e.visible and e.role in ("heading", "text", "cell") and e.text][:6]
        return self.redactor.scrub_text(f"url={self.surface.path()} title={snap.title!r} visible={texts}")

    # --- detectors -----------------------------------------------------------------------------------------

    def _first_hit(self, detectors: list[OutcomeDetector], snap: SemanticSnapshot, suppress: set[str]) -> OutcomeDetector | None:
        return first_hit(detectors, self.ev, snap, suppress)

    def _global_recoverable(self) -> list[OutcomeDetector]:
        return recoverable(self.profile.global_detectors)

    def _detail(self, det: BusinessDetector) -> str | None:
        if det.detail_locator is None:
            return None
        try:
            self.surface.observe()
            res = walk_ladder(self.surface, det.detail_locator, self.params)
            if res is None:
                return None
            return self.redactor.scrub_text(self.surface.read(res.element, "text"))
        except (ActError, KeyError):
            return None

    def _terminal_hit(self, det: OutcomeDetector, record: StepResult, expected: StateSignature | None, snap: SemanticSnapshot) -> None:
        """BUSINESS / HARD_FAILURE arms stop the run at once (no retry)."""
        if isinstance(det, BusinessDetector):
            record.status = StepStatus.BUSINESS_OUTCOME
            raise _Business(det, record.step_id, self._detail(det))
        if isinstance(det, HardFailureDetector):
            record.status = StepStatus.FAILED
            evidence = self._evidence(record, snap)
            raise _Failed(FailureKind.APP_ERROR, record.step_id, expected, f"{det.id}: {det.reason}; {self._summary(snap)}", evidence)

    def _recover(self, det: RecoverableDetector, record: StepResult, suppress: set[str], expected: StateSignature | None, snap: SemanticSnapshot) -> str:
        """Execute a RECOVERABLE detector's recovery, bounded by ``max_attempts`` per step per run.
        Returns "handled" | "failed" | "exhausted"."""
        rec = det.recovery
        if isinstance(rec, Escalate):
            # Default mode: an Escalate recovery is logged and treated as HARD_FAILURE (SPEC §9); under
            # --escalate the same _Failed becomes a TAKEOVER intervention in _main_step (§11).
            mode = "escalate" if self.escalation is not None else "default"
            self.log.event("recovery", {"step_id": record.step_id, "detector": det.id, "recovery": "escalate", "mode": mode, "ok": False})
            record.status = StepStatus.FAILED
            evidence = self._evidence(record, snap)
            why = f"{det.id}: detector requests human escalation" if mode == "escalate" else f"{det.id}: recovery is Escalate and the run is not in --escalate mode"
            raise _Failed(FailureKind.APP_ERROR, record.step_id, expected, why, evidence)
        key = (record.step_id, det.id)
        used = self._recovery_uses.get(key, 0)
        if used >= rec.max_attempts:
            self.log.event("recovery", {"step_id": record.step_id, "detector": det.id, "recovery": rec.kind, "attempt": used, "ok": False, "exhausted": True})
            return "exhausted"
        self._recovery_uses[key] = used + 1
        record.recoveries.append(det.id)
        nested = suppress | {det.id}
        ok = True
        if isinstance(rec, Dismiss):
            ok = self._dismiss(rec, record, nested)
        elif isinstance(rec, RunFlow):
            ok = self._run_flow(rec.flow_id, record, nested)
        elif isinstance(rec, RetryStep):
            ok = True  # the caller re-runs resolve → gate → act
        self.log.event("recovery", {"step_id": record.step_id, "detector": det.id, "recovery": rec.kind, "attempt": used + 1, "ok": ok})
        return "handled" if ok else "failed"

    def _dismiss(self, rec: Dismiss, record: StepResult, suppress: set[str]) -> bool:
        self.surface.observe()
        res = walk_ladder(self.surface, rec.target, self.params)
        if res is None:
            return False
        action = ConcreteAction(ActionType.CLICK, target=res.element)
        decision = self._gate(action, record.step_id, RiskClass.SAFE)
        if not isinstance(decision, Allow):
            return False
        self._log_action(record.step_id, action, note="dismiss")
        try:
            self.surface.act(action, 2000)
        except ActError as e:
            self.log.event("action_error", {"step_id": record.step_id, "kind": e.kind, "detail": e.detail})
            return False
        return True

    def _run_flow(self, flow_id: str, record: StepResult, suppress: set[str]) -> bool:
        """Run a profile flow; detector hits land on ``record`` (the invoking step); a flow-step
        miss is a failed recovery attempt, never a run-level CHECKPOINT_MISS."""
        self.log.event("flow", {"step_id": record.step_id, "flow_id": flow_id, "phase": "start"})
        for fstep in self.profile.flows[flow_id]:
            fsr = StepResult(step_id=fstep.id, status=StepStatus.PASSED, attempts=0, duration_ms=0)
            t0 = time.monotonic()
            outcome = self._execute_step(fstep, fsr, record, suppress=suppress, main_index=None)
            fsr.duration_ms = int((time.monotonic() - t0) * 1000)
            ok = outcome.kind == "passed"
            fsr.status = StepStatus.PASSED if ok else StepStatus.FAILED
            self.log.event("flow_step", {"step_id": record.step_id, "flow_id": flow_id, "flow_step": fsr.model_dump(mode="json")})
            if not ok:
                self._last_flow_failure = f"flow {flow_id!r} step {fstep.id!r}: {outcome.kind} — {outcome.observed}"
                self.log.event("flow", {"step_id": record.step_id, "flow_id": flow_id, "phase": "failed", "detail": self._last_flow_failure})
                return False
        self.log.event("flow", {"step_id": record.step_id, "flow_id": flow_id, "phase": "done"})
        return True

    # --- gate + act -----------------------------------------------------------------------------------------

    def _gate(self, action: ConcreteAction, step_id: str, step_risk: RiskClass, action_id: str | None = None) -> Allow | Deny | NeedsApproval:
        self._action_seq += 1
        ctx = GateContext(
            current_url=self.surface.url(),
            lease_state=self.lease_state,
            effective_url_patterns=self.bundle.effective_allowed_url_patterns,
            step_risk=step_risk,
            artifact_status=self.artifact.status,
            approve_risky=self.opts.approve_risky,
            action_id=action_id or f"{step_id}#{self._action_seq}",
            approvals=self.escalation.approvals if self.escalation is not None else None,
            secret_values=frozenset(self._secret_values),
            route_prefix=self.bundle.route_prefix,
        )
        decision = self.gate.check(action, ctx)
        data: dict[str, Any] = {
            "step_id": step_id, "action": action.action.value, "decision": decision.kind,
            "lease_state": self.lease_state.value, "owner": self.escalation.lease.owner if self.escalation is not None else "agent",
            "target": action.target.name if action.target is not None else None, "url": action.url,
        }
        if isinstance(decision, Allow):
            why = "SAFE" if decision.risk is RiskClass.SAFE else f"RISKY: {'policy rule ' + str(decision.rule) if decision.rule else 'declared'}"
            data.update({"reason": why, "risk": decision.risk.value, "approval_by": decision.approval_by, "rule": decision.rule})
        elif isinstance(decision, Deny):
            data.update({"reason": decision.reason.value, "detail": decision.detail})
        else:
            data.update({"reason": "APPROVAL_REQUIRED", "detail": decision.detail, "action_id": decision.action_id, "rule": decision.rule})
        self.log.event("gate", data)
        return decision

    def _log_action(self, step_id: str, action: ConcreteAction, note: str | None = None) -> None:
        t = action.target
        self.log.event(
            "action",
            {
                "step_id": step_id, "action": action.action.value, "note": note,
                "target": None if t is None else {"ref": t.ref, "role": t.role, "name": t.name, "frame_path": t.frame_path},
                "value": None if action.value is None else (action.value if action.value_kind != "secret" else "***"),
                "value_kind": action.value_kind, "url": action.url,
            },
        )

    def _value(self, step: StepSpec) -> tuple[str | None, str | None]:
        v = step.value
        if v is None:
            return None, None
        if isinstance(v, LiteralValue):
            return v.value, "literal"
        if isinstance(v, ParamRef):
            return self.params[v.name], "param"
        assert isinstance(v, SecretRef)
        secret = resolve_secret(v.name)
        self._secret_values.add(secret)
        self.redactor.add_sensitive_value(secret)
        return secret, "secret"

    def _deny(self, decision: Deny, step_id: str, expected: StateSignature | None, record: StepResult) -> None:
        if decision.reason is DenyReason.LEASE_NOT_HELD:
            # Internal error: logged, never executed (SPEC §8). Not a policy violation.
            self.log.event("internal_error", {"step_id": step_id, "reason": decision.reason.value, "detail": decision.detail})
            record.status = StepStatus.FAILED
            raise _Failed(FailureKind.CHECKPOINT_MISS, step_id, expected, f"internal error: {decision.reason.value}: {decision.detail}")
        record.status = StepStatus.FAILED
        raise _Failed(FailureKind.POLICY_VIOLATION, step_id, None, f"{decision.reason.value}: {decision.detail}", deny_reason=decision.reason)

    # --- escalation (SPEC §11) ----------------------------------------------------------------------------

    def _request(self, kind: str, step: StepSpec | None, step_id: str, reason: str, expected: StateSignature | None) -> InterventionRequest:
        assert self.escalation is not None
        try:
            shot = self.log.relative(self.surface.screenshot(self.profile.sensitive_elements))
        except Exception as e:  # noqa: BLE001
            self.log.event("evidence_error", {"step_id": step_id, "detail": str(e)[:200]})
            shot = ""
        snap = self.surface.observe()
        sig = f"{self.surface.path()}|{snap.title[:40]}|{hashlib.sha256(json.dumps([(e.ref, e.role, e.name) for e in snap.elements], default=str).encode()).hexdigest()[:16]}"
        target = step.target if step is not None else None
        return InterventionRequest(
            request_id=self.escalation.next_request_id(), run_id=self.escalation.run_id, kind=kind, capability_id=self.artifact.capability_id, goal=None,  # type: ignore[arg-type]
            step_id=step_id, reason=self.redactor.scrub_text(reason)[:500], screenshot=shot, page_signature=sig,
            target_fingerprint=target.fingerprint if target is not None else None, target_rationale=target.candidates[0].rationale if target is not None else None,
            expected=expected, console_url=self.escalation.console_url, requested_at=datetime.now(UTC), lease_state=LeaseState.PAUSED.value,
        )

    def _resume(self, index: int, sr: StepResult, record: InterventionRecord) -> int:
        """Hand-back: re-localize, don't assume. Returns the index to continue from; marks the steps the
        human completed; re-escalates once when nothing holds, then FAILED CHECKPOINT_MISS."""
        assert self.escalation is not None
        steps = self.artifact.steps
        for attempt in (1, 2):
            snap = self.surface.observe()
            self._log_snapshot(sr.step_id, snap, force=True)
            rp = resume_point(steps, self.artifact.success, lambda s, cur=snap: self.ev.signature_holds(s, cur), start=index)
            self.log.event("resume_point", {"step_id": sr.step_id, "start": index, "resume_to": rp, "after": "hand-back"})
            if rp is not None:
                if rp > index:
                    # §11/§5: the step whose postcondition the human satisfied is HUMAN_COMPLETED; the declared
                    # steps jumped over on the way (the escalated one included) are SKIPPED.
                    sr.status = StepStatus.HUMAN_COMPLETED if rp - 1 == index else StepStatus.SKIPPED
                    for j in range(index + 1, rp):
                        self.steps_out.append(StepResult(step_id=steps[j].id, status=StepStatus.HUMAN_COMPLETED if j == rp - 1 else StepStatus.SKIPPED, attempts=0, duration_ms=0))
                record.resumed_at_step = OUTPUTS if rp >= len(steps) else steps[rp].id
                self.escalation.request("resume", by="agent")
                return rp
            if attempt == 1:
                record.resolution = "resume_failed"
                req = self._request("TAKEOVER", steps[index], sr.step_id, "hand-back left no satisfied signature; the page matches no step's postcondition — take control again or abort", steps[index].postcondition)
                res, record2 = self.escalation.re_escalate(req)
                self.interventions.append(record2)
                if res != "resumed":
                    sr.status = StepStatus.BLOCKED
                    raise _Blocked(BlockedKind.ESCALATION_UNRESOLVED, sr.step_id, intervention=True)
                record = record2
        sr.status = StepStatus.FAILED
        self.escalation.request("resume", by="agent")  # the run ends under agent ownership
        snap = self.surface.observe()
        raise _Failed(FailureKind.CHECKPOINT_MISS, sr.step_id, steps[index].postcondition, f"after two human hand-backs no step postcondition or success signature holds; {self._summary(snap)}", self._evidence(sr, snap), escalated=True)

    def _takeover(self, index: int, sr: StepResult, reason: str) -> int:
        """TAKEOVER intervention for a FAILED-class terminal of a main step (§9 escalation mode)."""
        assert self.escalation is not None
        step = self.artifact.steps[index]
        req = self._request("TAKEOVER", step, sr.step_id, reason, step.postcondition)
        res, record = self.escalation.escalate(req)
        self.interventions.append(record)
        if res == "resumed":
            return self._resume(index, sr, record)
        sr.status = StepStatus.BLOCKED
        raise _Blocked(BlockedKind.ESCALATION_UNRESOLVED, sr.step_id, intervention=True)

    # --- one step (main or flow) -------------------------------------------------------------------------

    def _execute_step(self, step: StepSpec, sr: StepResult, record: StepResult, *, suppress: set[str], main_index: int | None) -> _StepOutcome:
        """SPEC §9 per-step algorithm. ``sr`` receives attempts/resolution; ``record`` receives detector
        hits and recoveries (== ``sr`` for a main step, the invoking step for a flow step)."""
        retries_left = step.retries
        source = "overlay" if main_index is not None and step.id in self.bundle.sources.step_targets else "artifact"
        risk = RiskClass.SAFE
        resolved_once = False
        snap = self.surface.observe()
        while True:
            sr.attempts += 1
            retry_requested = False
            # Pre-act: the profile's RECOVERABLE detectors once (a known interstitial is dismissed
            # proactively; one recovery + one max_attempts use, no resume_point).
            self._check_navigation(record, step.postcondition, step.id)
            snap = self._observe(record.step_id)
            hit = self._first_hit(self._global_recoverable(), snap, suppress)
            if isinstance(hit, RecoverableDetector):
                record.detectors_fired.append(hit.id)
                self.log.event("detector", {"step_id": record.step_id, "detector": hit.id, "arm": hit.arm, "phase": "pre-act"})
                self._recover(hit, record, suppress, step.postcondition, snap)
            # The step budget starts AFTER the pre-act recovery (a login flow must not eat it).
            deadline = time.monotonic() + step.wait_timeout_ms / 1000
            # Resolve the ladder, polled on the tick loop until the step deadline.
            res: WalkResult | None = None
            if step.target is not None:
                res = resolve_ladder_until(self.surface, step.target, self.params, deadline, source=source, tick_s=TICK_S, sleep=self.opts.sleep)
                snap = self.surface.last_snapshot()
                self._log_snapshot(record.step_id, snap, force=True)  # the snapshot the ladder resolved on
                if res is None:
                    attempts: list[str] = []
                    walk_ladder(self.surface, step.target, self.params, source=source, attempts_out=attempts)
                    observed = f"ladder unresolved after {step.wait_timeout_ms} ms: {attempts}; {self._summary(snap)}"
                    # LOCATOR_EXHAUSTED only when the ladder NEVER resolved (SPEC §9).
                    return _StepOutcome("locator" if not resolved_once else "checkpoint", observed=observed, expected=step.postcondition)
                resolved_once = True
                sr.resolution, sr.drift_flag = res.resolution, res.drift_flag
                self.log.event("resolve", {"step_id": record.step_id, "target_step": step.id, "tier": int(res.resolution.tier), "candidate_index": res.resolution.candidate_index, "source": source, "drift_flag": res.drift_flag, "ref": res.element.ref})
            else:
                self._log_snapshot(record.step_id, snap, force=True)
            value, value_kind = self._value(step)
            action = ConcreteAction(step.action, target=res.element if res else None, value=value, url=value if step.action is ActionType.NAVIGATE else None, value_kind=value_kind)  # type: ignore[arg-type]
            decision = self._gate(action, step.id, step.risk)
            if isinstance(decision, Deny):
                self._deny(decision, record.step_id, step.postcondition, record)
            if isinstance(decision, NeedsApproval):
                if self.escalation is None or main_index is None:
                    record.status = StepStatus.BLOCKED
                    raise _Blocked(BlockedKind.APPROVAL_REQUIRED, record.step_id)
                req = self._request("APPROVAL", step, record.step_id, f"RISKY step needs operator approval: {decision.detail}", step.postcondition)
                res, irec = self.escalation.escalate(req)
                self.interventions.append(irec)
                if res == "approved":
                    decision = self._gate(action, step.id, step.risk, action_id=req.request_id)  # the one-shot token is consumed here
                    if not isinstance(decision, Allow):
                        record.status = StepStatus.FAILED
                        raise _Failed(FailureKind.POLICY_VIOLATION, record.step_id, None, f"operator approval did not authorize the action: {getattr(decision, 'detail', decision)}")
                elif res == "denied":
                    record.status = StepStatus.BLOCKED
                    raise _Blocked(BlockedKind.APPROVAL_DENIED, record.step_id, intervention=True)
                elif res == "resumed":
                    return _StepOutcome("jump", resume_to=self._resume(main_index, record, irec))
                else:
                    record.status = StepStatus.BLOCKED
                    raise _Blocked(BlockedKind.ESCALATION_UNRESOLVED, record.step_id, intervention=True)
            assert isinstance(decision, Allow)
            risk = decision.risk
            if decision.approval_by:
                self.log.event("approval_used", {"step_id": step.id, "by": decision.approval_by, "rule": decision.rule})
            url_before = self.surface.url()
            self._log_action(record.step_id, action)
            t_act = time.monotonic()
            try:
                self.surface.act(action, step.wait_timeout_ms)
            except ActError as e:  # a tick at t=0 with the postcondition false
                self.log.event("action_error", {"step_id": record.step_id, "kind": e.kind, "detail": e.detail})
            del url_before
            # Tick loop: (a) postcondition, (b) step detectors, (c) global detectors; first to hold wins.
            # A navigation may commit on any later tick (clicks never wait), so layer 2 runs every tick.
            while True:
                self._check_navigation(record, step.postcondition, step.id)
                snap = self._observe(record.step_id)
                if self.ev.signature_holds(step.postcondition, snap):
                    self.log.event("postcondition", {"step_id": record.step_id, "target_step": step.id, "holds": True, "elapsed_ms": int((time.monotonic() - t_act) * 1000)})
                    return _StepOutcome("passed")
                hit = self._first_hit(step.detectors, snap, suppress) or self._first_hit(self.profile.global_detectors, snap, suppress)
                if hit is not None:
                    if not record.detectors_fired or record.detectors_fired[-1] != hit.id:
                        record.detectors_fired.append(hit.id)
                    self.log.event("detector", {"step_id": record.step_id, "detector": hit.id, "arm": hit.arm, "elapsed_ms": int((time.monotonic() - t_act) * 1000)})
                    if not isinstance(hit, RecoverableDetector):
                        self._terminal_hit(hit, record, step.postcondition, snap)
                    state = self._recover(hit, record, suppress, step.postcondition, snap)
                    if state == "exhausted":
                        break  # treated as "nothing matched" → retry policy below
                    if isinstance(hit.recovery, RetryStep):
                        if risk is RiskClass.RISKY:
                            # Never re-act on an effective-RISKY step (SPEC §9): fall to "ambiguous state".
                            self.log.event("recovery", {"step_id": record.step_id, "detector": hit.id, "recovery": "retry_step", "ok": False, "refused": "effective risk RISKY"})
                            break
                        retry_requested = state == "handled"
                        break
                    if state == "handled" and main_index is not None:
                        snap = self._observe(record.step_id)
                        rp = resume_point(self.artifact.steps, self.artifact.success, lambda s, cur=snap: self.ev.signature_holds(s, cur), start=main_index)
                        self.log.event("resume_point", {"step_id": step.id, "start": main_index, "resume_to": rp})
                        if rp is None:
                            continue  # the recovery attempt failed (counted); keep polling
                        if rp == main_index + 1:
                            return _StepOutcome("passed")
                        return _StepOutcome("jump", resume_to=rp)
                    continue
                if time.monotonic() >= deadline:
                    break
                self.opts.sleep(TICK_S)
            self.log.event("postcondition", {"step_id": record.step_id, "target_step": step.id, "holds": False, "elapsed_ms": int((time.monotonic() - t_act) * 1000)})
            if retry_requested:
                continue
            if retries_left > 0 and risk is RiskClass.SAFE:
                retries_left -= 1
                self.log.event("retry", {"step_id": record.step_id, "target_step": step.id, "remaining": retries_left})
                continue
            if risk is RiskClass.RISKY:
                return _StepOutcome("checkpoint", observed="ambiguous state after irreversible action; " + self._summary(snap), expected=step.postcondition)
            return _StepOutcome("checkpoint", observed=f"postcondition not met within {step.wait_timeout_ms} ms (retries exhausted); {self._summary(snap)}", expected=step.postcondition)

    # --- phases -------------------------------------------------------------------------------------------------

    def _poll_signature(self, sig: StateSignature, record: StepResult, deadline: float, *, one_tick: bool = False) -> bool:
        """Tick loop for the pseudo-steps: signature, then global detectors (all arms)."""
        while True:
            self._check_navigation(record, sig)
            snap = self._observe(record.step_id)
            if self.ev.signature_holds(sig, snap):
                return True
            hit = self._first_hit(self.profile.global_detectors, snap, set())
            if hit is not None:
                if not record.detectors_fired or record.detectors_fired[-1] != hit.id:
                    record.detectors_fired.append(hit.id)
                self.log.event("detector", {"step_id": record.step_id, "detector": hit.id, "arm": hit.arm})
                if not isinstance(hit, RecoverableDetector):
                    self._terminal_hit(hit, record, sig, snap)
                if self._recover(hit, record, set(), sig, snap) != "exhausted":
                    continue  # simply re-poll (no resume_point) — SPEC §9
            if one_tick or time.monotonic() >= deadline:
                return False
            self.opts.sleep(TICK_S)

    def _preconditions(self) -> None:
        sr = StepResult(step_id=PRECONDITIONS, status=StepStatus.PASSED, attempts=1, duration_ms=0)
        self.steps_out.append(sr)
        t0 = time.monotonic()
        first_timeout = self.artifact.steps[0].wait_timeout_ms
        try:
            # Entry navigation is an action like any other: it passes the gate first.
            action = ConcreteAction(ActionType.NAVIGATE, url=self.bundle.entry_url)
            decision = self._gate(action, PRECONDITIONS, RiskClass.SAFE)
            if isinstance(decision, Deny):
                self._deny(decision, PRECONDITIONS, self.artifact.preconditions, sr)
            if isinstance(decision, NeedsApproval):
                sr.status = StepStatus.BLOCKED
                raise _Blocked(BlockedKind.APPROVAL_REQUIRED, PRECONDITIONS)
            self._log_action(PRECONDITIONS, action)
            self._checked_url = self.surface.url()  # about:blank — nothing to check until the URL changes
            entry_error: ActError | None = None
            try:
                self.surface.act(action, first_timeout)
            except ActError as e:
                entry_error = e  # an unreachable target is a CHECKPOINT_MISS, not a policy violation
                self.log.event("action_error", {"step_id": PRECONDITIONS, "kind": e.kind, "detail": e.detail})
            pre = self.artifact.preconditions
            if entry_error is not None and self.surface.url() == self._checked_url:
                sr.status = StepStatus.FAILED
                snap = self.surface.observe()
                raise _Failed(FailureKind.CHECKPOINT_MISS, PRECONDITIONS, pre, f"entry navigation to {self.bundle.entry_url} failed: {entry_error.kind}: {entry_error.detail}", self._evidence(sr, snap))
            if self._poll_signature(pre, sr, time.monotonic(), one_tick=True):
                return
            for flow_id in self.artifact.requires_flows:
                if not self._run_flow(flow_id, sr, set()):
                    sr.status = StepStatus.FAILED
                    snap = self.surface.observe()
                    raise _Failed(FailureKind.CHECKPOINT_MISS, PRECONDITIONS, pre, f"{self._last_flow_failure}; {self._summary(snap)}", self._evidence(sr, snap))
            if not self._poll_signature(pre, sr, time.monotonic() + first_timeout / 1000):
                sr.status = StepStatus.FAILED
                snap = self.surface.observe()
                raise _Failed(FailureKind.CHECKPOINT_MISS, PRECONDITIONS, pre, f"preconditions still false after flows {self.artifact.requires_flows}; {self._summary(snap)}", self._evidence(sr, snap))
        finally:
            sr.duration_ms = int((time.monotonic() - t0) * 1000)

    def _main_step(self, index: int) -> int:
        """Run declared step ``index``; return the index to continue from."""
        step = self.artifact.steps[index]
        sr = StepResult(step_id=step.id, status=StepStatus.PASSED, attempts=0, duration_ms=0)
        self.steps_out.append(sr)
        t0 = time.monotonic()
        try:
            try:
                outcome = self._execute_step(step, sr, sr, suppress=set(), main_index=index)
            except _Failed as f:
                if self.escalation is None or f.kind is FailureKind.POLICY_VIOLATION or f.escalated:
                    raise
                # HARD_FAILURE detector / Escalate recovery / internal miss under --escalate: TAKEOVER (§9, §11)
                return self._takeover(index, sr, f"{f.kind.value}: {f.observed}")
        finally:
            sr.duration_ms = int((time.monotonic() - t0) * 1000)
        if outcome.kind == "passed":
            return index + 1
        if outcome.kind == "jump":
            assert outcome.resume_to is not None
            done = {s.step_id for s in self.steps_out if s.status in (StepStatus.HUMAN_COMPLETED, StepStatus.SKIPPED)}
            for j in range(index + 1, outcome.resume_to):
                if self.artifact.steps[j].id not in done:
                    self.steps_out.append(StepResult(step_id=self.artifact.steps[j].id, status=StepStatus.SKIPPED, attempts=0, duration_ms=0))
            return outcome.resume_to
        kind = FailureKind.LOCATOR_EXHAUSTED if outcome.kind == "locator" else FailureKind.CHECKPOINT_MISS
        if self.escalation is not None:
            t1 = time.monotonic()
            try:
                return self._takeover(index, sr, f"{kind.value}: {outcome.observed}")
            finally:
                sr.duration_ms += int((time.monotonic() - t1) * 1000)
        sr.status = StepStatus.FAILED
        snap = self.surface.observe()
        evidence = self._evidence(sr, snap)
        raise _Failed(kind, step.id, outcome.expected, outcome.observed, evidence)

    def _success(self) -> None:
        sr = StepResult(step_id=SUCCESS, status=StepStatus.PASSED, attempts=1, duration_ms=0)
        self.steps_out.append(sr)
        t0 = time.monotonic()
        last_timeout = self.artifact.steps[-1].wait_timeout_ms
        try:
            if not self._poll_signature(self.artifact.success, sr, time.monotonic() + last_timeout / 1000):
                snap = self.surface.observe()
                observed = f"success signature not met within {last_timeout} ms; {self._summary(snap)}"
                if self.escalation is not None:
                    req = self._request("TAKEOVER", None, SUCCESS, f"CHECKPOINT_MISS: {observed}", self.artifact.success)
                    res, record = self.escalation.escalate(req)
                    self.interventions.append(record)
                    for attempt in (1, 2):
                        if res != "resumed":
                            sr.status = StepStatus.BLOCKED
                            raise _Blocked(BlockedKind.ESCALATION_UNRESOLVED, SUCCESS, intervention=True)
                        again = self.surface.observe()
                        self._log_snapshot(SUCCESS, again, force=True)
                        if self.ev.signature_holds(self.artifact.success, again):
                            record.resumed_at_step = OUTPUTS
                            sr.status = StepStatus.HUMAN_COMPLETED
                            self.escalation.request("resume", by="agent")
                            return
                        if attempt == 1:
                            record.resolution = "resume_failed"
                            req = self._request("TAKEOVER", None, SUCCESS, "hand-back left the success signature unsatisfied — take control again or abort", self.artifact.success)
                            res, record = self.escalation.re_escalate(req)
                            self.interventions.append(record)
                    sr.status = StepStatus.FAILED
                    self.escalation.request("resume", by="agent")
                    raise _Failed(FailureKind.CHECKPOINT_MISS, SUCCESS, self.artifact.success, "after two human hand-backs the success signature still does not hold; " + self._summary(self.surface.observe()), self._evidence(sr), escalated=True)
                sr.status = StepStatus.FAILED
                raise _Failed(FailureKind.CHECKPOINT_MISS, SUCCESS, self.artifact.success, observed, self._evidence(sr, snap))
        finally:
            sr.duration_ms = int((time.monotonic() - t0) * 1000)

    def _outputs(self) -> dict[str, str | int | float | bool]:
        sr = StepResult(step_id=OUTPUTS, status=StepStatus.PASSED, attempts=1, duration_ms=0)
        self.steps_out.append(sr)
        t0 = time.monotonic()
        deadline = time.monotonic() + self.artifact.steps[-1].wait_timeout_ms / 1000
        values: dict[str, str | int | float | bool] = {}
        try:
            for out in self.artifact.outputs:
                source = "overlay" if out.name in self.bundle.sources.outputs else "artifact"
                res = resolve_ladder_until(self.surface, out.extraction.locator, self.params, deadline, source=source, tick_s=TICK_S, sleep=self.opts.sleep)
                if res is None:
                    sr.status = StepStatus.FAILED
                    snap = self.surface.last_snapshot()
                    raise _Failed(FailureKind.CHECKPOINT_MISS, OUTPUTS, self.artifact.success, f"output {out.name!r}: extraction ladder unresolved; {self._summary(snap)}", self._evidence(sr, snap))
                raw = self.surface.read(res.element, out.extraction.attr)  # LIVE, never the redacted snapshot
                if out.sensitive:
                    self.redactor.add_sensitive_value(raw)
                try:
                    values[out.name] = coerce_output(out, raw)
                    if out.sensitive:
                        self.redactor.add_sensitive_value(str(values[out.name]))
                except ValueError as e:
                    sr.status = StepStatus.FAILED
                    snap = self.surface.last_snapshot()
                    raise _Failed(FailureKind.CHECKPOINT_MISS, OUTPUTS, self.artifact.success, self.redactor.scrub_text(f"output {out.name!r}: {e}; observed {raw!r}"), self._evidence(sr, snap)) from e
                self.log.event("output", {"name": out.name, "tier": int(res.resolution.tier), "source": source, "value": "***" if out.sensitive else values[out.name]})
        finally:
            sr.duration_ms = int((time.monotonic() - t0) * 1000)
        return values

    def execute(self) -> dict[str, str | int | float | bool]:
        self._preconditions()
        i = 0
        n = len(self.artifact.steps)
        while i < n:
            i = self._main_step(i)
        self._success()
        return self._outputs()


# --- the run --------------------------------------------------------------------------------------------------------


def _headless(opts: ReplayOptions) -> bool:
    if opts.headless is not None:
        return opts.headless
    return os.environ.get("ANTON_HEADLESS", "") == "1"


def _launch(opts: ReplayOptions, run_dir: Path, record_binding: Callable[[Any, dict[str, Any]], None] | None = None) -> tuple[Any, Any, Any]:
    from playwright.sync_api import sync_playwright  # imported here: the engine module stays driver-free

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=_headless(opts))
    kwargs: dict[str, Any] = {"viewport": {"width": opts.viewport[0], "height": opts.viewport[1]}}
    if opts.record_video:
        kwargs["record_video_dir"] = str(run_dir / "video")
    context = browser.new_context(**kwargs)
    if record_binding is not None:
        from anton.escalation.recorder import (
            install_recorder,  # at context creation, before any navigation (§11)
        )

        install_recorder(context, record_binding)
    return pw, browser, context


def run_replay(bundle: LoadedBundle, params: Mapping[str, str], opts: ReplayOptions | None = None, *, run_id: str | None = None) -> ReplayResult:
    """SPEC §9 start-up → run → result. Params are already validated; the loader has run.
    Returns the REDACTED view (``redacted_view=True``); only ``opts.outputs_file`` carries raw
    sensitive outputs."""
    opts = opts or ReplayOptions()
    run_id = run_id or new_run_id()
    started = datetime.now(UTC)
    run_dir = opts.run_dir if opts.run_dir is not None else (opts.runs_root or repo_root() / "runs") / run_id
    artifact, policy = bundle.artifact, bundle.policy
    sensitive_params = [params[p.name] for p in artifact.inputs if p.sensitive and p.name in params]
    env_secrets = {v for k, v in os.environ.items() if k.startswith("ANTON_SECRET_") and v}
    redactor = Redactor(policy.redact_field_patterns, policy.redact_value_patterns, known_values=set(sensitive_params) | env_secrets)
    log = RunLog(run_dir, redactor)
    gate = ActionGate(policy)
    steps: list[StepResult] = []
    terminal: _Terminal | None = None
    outputs: dict[str, str | int | float | bool] | None = None
    interventions: list[InterventionRecord] = []
    controller: Any | None = None
    log.event("run", {"run_id": run_id, "capability_id": artifact.capability_id, "version": artifact.version, "tenant_id": bundle.tenant_id, "overlay_id": bundle.overlay.overlay_id if bundle.overlay else None, "entry_url": bundle.entry_url, "params": {k: ("***" if k in {p.name for p in artifact.inputs if p.sensitive} else v) for k, v in params.items()}, "approve_risky": opts.approve_risky, "allow_draft": opts.allow_draft, "escalate": opts.escalate})
    risky = artifact.risky_steps()
    deny = gate.draft_check(artifact.status, opts.allow_draft, risky[0].id if risky else None)
    if deny is not None:
        # Start-up refusal: no browser is launched (SPEC §8, AW-7e).
        log.event("gate", {"step_id": None, "decision": "Deny", "reason": deny.reason.value, "detail": deny.detail, "lease_state": LeaseState.AGENT_ACTIVE.value, "owner": "agent", "phase": "start-up"})
        terminal = _Failed(FailureKind.POLICY_VIOLATION, risky[0].id if risky else None, None, f"{deny.reason.value}: {deny.detail}")
    else:
        from anton.surface.web import PlaywrightSurface

        if opts.escalate:
            from anton.escalation.controller import EscalationController

            controller = EscalationController(run_dir, run_id, redactor, log, console_port=opts.console_port, timeout_s=opts.escalate_timeout_s, capability_id=artifact.capability_id)
        pw, browser, context = _launch(opts, run_dir, record_binding=controller.record_binding if controller is not None else None)
        try:
            page = context.new_page()
            if controller is not None:
                from anton.escalation.recorder import arm_loaded_frames

                controller.pump = lambda ms: page.wait_for_timeout(ms)  # the only sanctioned wait while parked (§11)
                controller.on_take = lambda: (page.bring_to_front(), arm_loaded_frames(page))
            surface = PlaywrightSurface(page, redactor, bundle.profile.sensitive_elements, bundle.route_prefix, screenshot_dir=run_dir / "screenshots", on_event=log.event)
            engine = ReplayEngine(bundle, params, opts, surface, gate, log, redactor, steps, escalation=controller)
            engine._secret_values |= env_secrets
            interventions = engine.interventions
            try:
                outputs = engine.execute()
            except _Terminal as t:
                terminal = t
            except Exception as e:  # noqa: BLE001 - a driver/engine crash is still a FAILED result with evidence, never a traceback
                log.event("engine_error", {"type": type(e).__name__, "detail": str(e)[:300]})
                last = steps[-1] if steps else None
                evidence: list[str] = []
                if last is not None:
                    last.status = StepStatus.FAILED
                    evidence = engine._evidence(last)
                terminal = _Failed(FailureKind.CHECKPOINT_MISS, last.step_id if last else None, None, redactor.scrub_text(f"engine error: {type(e).__name__}: {str(e)[:200]}"), evidence)
            offlist = isinstance(terminal, _Failed) and terminal.deny_reason is DenyReason.POST_NAV_OFFLIST
            if not offlist:  # SPEC §8: an off-allowlist page is never observed or screenshotted
                try:
                    final = surface.screenshot(bundle.profile.sensitive_elements)
                    log.event("screenshot", {"phase": "final", "path": log.relative(final)})
                except Exception as e:  # noqa: BLE001
                    log.event("evidence_error", {"step_id": None, "detail": str(e)[:200]})
        finally:
            if controller is not None:
                controller.close()
            for closer in (context.close, browser.close, pw.stop):
                try:
                    closer()
                except Exception:  # noqa: BLE001, S110 - best-effort teardown
                    pass

    finished = datetime.now(UTC)
    lease_state = controller.lease.state.value if controller is not None else LeaseState.AGENT_ACTIVE.value
    base: dict[str, Any] = {
        "run_id": run_id, "capability_id": artifact.capability_id, "version": artifact.version, "tenant_id": bundle.tenant_id,
        "overlay_id": bundle.overlay.overlay_id if bundle.overlay else None, "artifact_sha256": content_hash(bundle.base),
        "steps": steps, "interventions": interventions, "started_at": started, "finished_at": finished, "redacted_view": True,
    }
    if terminal is None:
        result = ReplayResult(status=ReplayStatus.SUCCESS, outputs=outputs or {}, **base)
    elif isinstance(terminal, _Business):
        result = ReplayResult(status=ReplayStatus.BUSINESS_OUTCOME, outcome_code=terminal.detector.outcome_code, outcome_step_id=terminal.step_id, outcome_detector_id=terminal.detector.id, outcome_detail=terminal.detail, **base)
    elif isinstance(terminal, _Blocked):
        result = ReplayResult(status=ReplayStatus.BLOCKED, blocked=BlockedDetail(kind=terminal.kind, step_id=terminal.step_id, intervention_request_path="intervention.json" if terminal.intervention else None, lease_state=lease_state), **base)
    else:
        assert isinstance(terminal, _Failed)
        result = ReplayResult(status=ReplayStatus.FAILED, failure=FailureDetail(kind=terminal.kind, step_id=terminal.step_id, expected=terminal.expected, observed_summary=redactor.scrub_text(terminal.observed), evidence=terminal.evidence), **base)
    outputs_error: OSError | None = None
    if opts.outputs_file is not None and result.status is ReplayStatus.SUCCESS:
        try:
            write_outputs_file(Path(opts.outputs_file), outputs or {})
        except OSError as e:
            outputs_error = e
            log.event("outputs_error", {"path": str(opts.outputs_file), "detail": str(e)[:200]})
    view = result.model_dump(mode="json")
    if view.get("outputs") is not None:
        # Sensitive outputs are masked by NAME in the redacted view (never by matching their value).
        view["outputs"] = {k: ("***" if k in {o.name for o in artifact.outputs if o.sensitive} else v) for k, v in view["outputs"].items()}
    redacted = ReplayResult.model_validate(redactor.scrub(view))
    log.write_json("result.json", redacted)
    log.event("result", {"status": redacted.status.value, "run_dir": _display_dir(run_dir)})
    log.close()
    if outputs_error is not None:
        raise OSError(f"run {run_id} finished {redacted.status.value} but --outputs-file could not be written: {outputs_error}") from outputs_error
    return redacted


def _display_dir(path: Path) -> str:
    """Repo-root-relative when inside the repo — a run.jsonl never carries a machine-specific path."""
    try:
        return str(Path(path).resolve().relative_to(repo_root().resolve()))
    except ValueError:
        return Path(path).name


def check_outputs_path(path: Path) -> Path:
    """SPEC §5: the raw outputs file is never placed under ``runs/`` or ``evidence/``; the parent
    must exist and be writable (checked BEFORE the run so a bad path is a usage error, exit 2)."""
    resolved = Path(path).resolve()
    root = repo_root().resolve()
    for forbidden in (root / "runs", root / "evidence"):
        if resolved == forbidden or forbidden in resolved.parents:
            raise ParamError(f"--outputs-file must not be placed under {forbidden.relative_to(root)}/ (it carries raw sensitive outputs)")
    if not resolved.parent.is_dir():
        raise ParamError(f"--outputs-file parent directory does not exist: {resolved.parent}")
    if not os.access(resolved.parent, os.W_OK):
        raise ParamError(f"--outputs-file parent directory is not writable: {resolved.parent}")
    return resolved


def write_outputs_file(path: Path, outputs: Mapping[str, Any]) -> None:
    """Raw outputs, mode 0600 whether the file is new or pre-existing."""
    import json

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dict(outputs), f, indent=2)
            f.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
