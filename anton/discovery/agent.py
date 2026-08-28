"""Discovery agent (SPEC §10.1, §10.2) — the only module that talks to the model.

The harness logs in THROUGH THE REPLAY ENGINE before the first model turn (the model never sees
credentials), then loops: redacted digest (≤ ~60 elements) + masked screenshot + goal + declared
params/outputs + a short history → ONE tool call per turn (``act`` / ``read`` / ``finish`` /
``give_up`` / ``ask_human``). Every ``act`` passes ``ActionGate.check``; the post-navigation check runs
after every action. Each turn is recorded as a ``TurnRecord`` in ``turns.jsonl`` with its full
redacted snapshot under ``digests/``; scrubbed request/response payloads go to ``transcript.jsonl``.
On ``finish`` the harness reads the cited outputs itself and the distiller (§10.3) writes the
draft artifact. Stop conditions: ``finish``, ``max_steps=30``, wall clock 5 min, or dead-end (§10.1).

Model id from ``ANTON_MODEL`` (default ``claude-sonnet-5``); adaptive thinking; no ``budget_tokens``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anton.discovery.describe import describe
from anton.distill.distiller import DistillContext, DistillError, RecordedTurn, distill
from anton.evidence.log import RunLog
from anton.policy.gate import ActionGate, Allow, Deny, DenyReason, NeedsApproval, PolicyConfig, resolve_secret
from anton.policy.redact import Redactor
from anton.replay.engine import ReplayEngine, ReplayOptions, _Failed, _launch, _Terminal, new_run_id
from anton.schema.app_profile import AppProfile
from anton.schema.artifact import (
    ActionType,
    Approval,
    CapabilityArtifact,
    ElementVisible,
    LiteralValue,
    ParamRef,
    Provenance,
    RecordedAgainst,
    RiskClass,
    SecretRef,
    StateSignature,
    StepSpec,
    UrlMatches,
    ValueRef,
    content_hash,
)
from anton.schema.loader import (
    LoadedBundle,
    OverlaySources,
    _compose_entry_url,
    check_profile_subset,
    load_bundle,
    repo_root,
)
from anton.schema.result import BlockedDetail, BlockedKind, DiscoveryResult, FailureKind
from anton.schema.snapshot import SemanticSnapshot, TurnRecord
from anton.schema.spec import CapabilitySpec
from anton.surface.base import ConcreteAction, Surface

__all__ = ["DEFAULT_MODEL", "DiscoverOptions", "Discovery", "discovery_bundle", "page_signature", "digest_of", "TOOLS", "run_discovery"]

DEFAULT_MODEL = "claude-sonnet-5"
DIGEST_LIMIT = 60
ACT_TIMEOUT_MS = 8000

TOOLS: list[dict[str, Any]] = [
    {
        "name": "act",
        "description": "Perform ONE UI action. CLICK/FILL/SELECT/PRESS on a digest element need its ref; NAVIGATE (value = URL) and a global PRESS (value = key name) use ref = null. FILL/SELECT values may be a literal, \"$param:<name>\" for a declared param, or \"$secret:<name>\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": ["string", "null"], "description": "element ref from THIS turn's digest, e.g. \"e12\"; null only for NAVIGATE / global PRESS"},
                "action": {"type": "string", "enum": ["CLICK", "FILL", "SELECT", "PRESS", "NAVIGATE"]},
                "value": {"type": ["string", "null"], "description": "text to type / option label to select / key name / URL"},
                "rationale": {"type": "string", "description": "one sentence: why this action moves toward the goal"},
            },
            "required": ["ref", "action", "rationale"],
        },
    },
    {
        "name": "read",
        "description": "Return the (redacted) text of one element of THIS turn's digest. Use it to confirm a value before finishing.",
        "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "rationale": {"type": "string"}}, "required": ["ref", "rationale"]},
    },
    {
        "name": "finish",
        "description": "Declare the goal reached. For EVERY declared output give the ref of the element whose text IS that value (for a table: the cell in the right row AND column). The harness reads the value itself — never transcribe it.",
        "input_schema": {
            "type": "object",
            "properties": {"outputs": {"type": "object", "additionalProperties": {"type": "string"}, "description": "{output name: ref}"}, "rationale": {"type": "string"}},
            "required": ["outputs", "rationale"],
        },
    },
    {
        "name": "give_up",
        "description": "Stop: the goal cannot be reached from here (explain why).",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "ask_human",
        "description": "Ask a human operator to take over (only useful when the system prompt says a human is available).",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
]

INTERACTIVE = ("button", "link", "textbox", "checkbox", "radio", "combobox")


def digest_of(snap: SemanticSnapshot) -> list[dict[str, Any]]:
    """≤ ~60 visible elements: interactive first, then text-bearing static elements (SPEC §10.1)."""
    visible = [e for e in snap.elements if e.visible]
    interactive = [e for e in visible if e.role in INTERACTIVE]
    static = [e for e in visible if e.role not in INTERACTIVE and e.text.strip()]
    out = []
    for e in (interactive + static)[:DIGEST_LIMIT]:
        d: dict[str, Any] = {"ref": e.ref, "role": e.role, "name": e.name, "text": e.text[:120]}
        if e.frame_path:
            d["frame"] = "/".join(e.frame_path)
        if e.value is not None:
            d["value"] = e.value
        if e.checked is not None:
            d["checked"] = e.checked
        out.append(d)
    return out


def page_signature(snap: SemanticSnapshot, path: str) -> str:
    """Canonical URL path + landmark text + sha256 of the (ref, role, name, value, checked, enabled) tuples."""
    heads = [e for e in snap.elements if e.visible and e.role == "heading" and e.text.strip()]
    texts = [e for e in snap.elements if e.visible and e.role == "text" and e.text.strip() and e.bbox.y > 40]  # below the header band
    landmark = (heads or texts or [None])[0]
    landmark = landmark.text.strip() if landmark is not None else ""
    tuples = [(e.ref, e.role, e.name, e.value, e.checked, e.enabled) for e in snap.elements]
    digest = hashlib.sha256(json.dumps(tuples, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"{path}|{landmark[:40]}|{digest}"


def value_ref(value: str | None) -> ValueRef | None:
    if value is None:
        return None
    if value.startswith("$param:"):
        return ParamRef(name=value[len("$param:"):])
    if value.startswith("$secret:"):
        return SecretRef(name=value[len("$secret:"):])
    return LiteralValue(value=value)


def _dump_block(b: Any) -> Any:
    if isinstance(b, dict):
        return b
    if hasattr(b, "model_dump"):
        return b.model_dump()
    return dict(vars(b))


def _serialize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Transcript view of the request: image bytes become a size stub; SDK blocks become dicts."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        items = []
        for c in content:
            d = _dump_block(c)
            if d.get("type") == "image":
                items.append({"type": "image", "bytes": len((d.get("source") or {}).get("data", ""))})
            else:
                items.append(d)
        out.append({"role": m["role"], "content": items})
    return out


@dataclass
class DiscoverOptions:
    goal: str
    spec: CapabilitySpec
    params: dict[str, str]
    profile_path: str
    out_dir: Path
    approve_risky: bool = False
    escalate: bool = False
    escalate_timeout_s: int = 600
    console_port: int = 7071
    record_video: bool = False
    headless: bool | None = None
    model: str = field(default_factory=lambda: os.environ.get("ANTON_MODEL", DEFAULT_MODEL))
    max_steps: int = 30
    wall_clock_s: int = 300
    sleep: Callable[[float], None] = time.sleep


def discovery_bundle(profile: AppProfile, policy: PolicyConfig, url: str | None, profile_path: str) -> LoadedBundle:
    """A synthetic bundle whose artifact exists only to drive the engine's login/preconditions:
    preconditions = entry path + the profile's own authenticated landmark (§10.3 rule 6)."""
    entry_url = _compose_entry_url(url, None, profile)
    route_prefix = profile.route_prefix
    patterns = [route_prefix + p for p in profile.allowed_url_patterns]
    check_profile_subset(patterns, policy)
    from urllib.parse import urlsplit

    from anton.distill.distiller import canonical_path_regex

    landmark = next(p for p in profile.flows["login"][-1].postcondition.all_of if isinstance(p, ElementVisible))
    pre = StateSignature(all_of=[UrlMatches(regex=canonical_path_regex(urlsplit(entry_url).path or "/", {}, route_prefix)), landmark])
    dummy = StepSpec(id="discovery_placeholder", description="never executed; carries the poll timeout", action=ActionType.PRESS, value=LiteralValue(value="Tab"), postcondition=pre)
    art = CapabilityArtifact(
        capability_id="servicing.discovery_session", name="discovery session", description="synthetic: login + preconditions only", version="1.0.0",
        app_profile=profile_path, preconditions=pre, inputs=[], outputs=[], business_outcomes={}, steps=[dummy], success=pre,
        provenance=Provenance(origin="hand_authored", created_at=datetime.now(UTC), recorded_against=RecordedAgainst(app_id=profile.app_id, tenant_id=profile.tenant_id, app_version=profile.app_version, entry_url=entry_url)),
    )
    # Discovery has no artifact to approve: the §8 draft rules are about REPLAYING unapproved artifacts.
    # RISKY actions here are governed by --approve-risky / the operator (§10.1), so the synthetic
    # bundle is approved by the harness itself (hash-bound like any approval).
    art = art.model_copy(update={"status": "approved", "approval": Approval(approved_at=datetime.now(UTC), approved_by="discovery-harness", artifact_sha256=content_hash(art))})
    return LoadedBundle(base=art, artifact=art, profile=profile, policy=policy, overlay=None, sources=OverlaySources(), route_prefix=route_prefix, entry_url=entry_url, effective_allowed_url_patterns=patterns)


class Discovery:
    def __init__(self, opts: DiscoverOptions, bundle: LoadedBundle, run_dir: Path, *, client: Any | None = None,
                 surface_factory: Callable[[Path, Redactor, RunLog], tuple[Surface, Callable[[], None]]] | None = None, login: bool = True) -> None:
        self.opts, self.bundle, self.run_dir = opts, bundle, run_dir
        policy = bundle.policy
        self.redactor = Redactor(policy.redact_field_patterns, policy.redact_value_patterns,
                                 known_values={opts.params[p.name] for p in opts.spec.inputs if p.sensitive and p.name in opts.params})
        self.log = RunLog(run_dir, self.redactor)
        self.gate = ActionGate(policy)
        self._client = client
        self._surface_factory = surface_factory
        self._login = login
        self.turns_path = run_dir / "turns.jsonl"
        self.transcript_path = run_dir / "transcript.jsonl"
        (run_dir / "digests").mkdir(exist_ok=True)
        self.read_values: list[str] = []
        self.secret_values: set[str] = {v for k, v in os.environ.items() if k.startswith("ANTON_SECRET_") and v}
        self.redactor.add_sensitive_values(self.secret_values)

    # --- plumbing ---------------------------------------------------------------------------------------

    def _append(self, path: Path, obj: Any) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.redactor.scrub(json.loads(json.dumps(obj, default=str)))) + "\n")

    def _system(self) -> str:
        o = self.opts
        parts = []
        for p in o.spec.inputs:
            shown_value = f'(sensitive; type "$param:{p.name}")' if p.sensitive else str(o.params.get(p.name, ""))
            parts.append(f"{p.name}={shown_value} — {p.description}")
        shown = ", ".join(parts)
        outs = "; ".join(f"{d.name} ({d.type.value}): {d.description}" for d in o.spec.outputs)
        human = "A human operator IS available via ask_human." if o.escalate else "NO human is available: ask_human is treated as give_up."
        return (
            "You operate a legacy back-office web console through a strict harness. You are ALREADY authenticated and "
            f"standing on the entry page.\nGOAL: {o.goal}\n"
            f"Declared params: {shown}\nDeclared outputs you must cite in finish: {outs}\n"
            "Each turn you receive a digest of the visible elements (refs like e12 are valid ONLY for that turn) and a masked "
            "screenshot. Call exactly ONE tool per turn. Type \"$param:<name>\" as the FILL value for a declared param. "
            "Use read to confirm a cell's text before finishing. When every declared output's value is visible, call finish "
            f"citing the ref of the element whose text IS the value. Never transcribe values. {human} "
            "If the goal is impossible, call give_up with the reason."
        )

    def _default_surface(self, run_dir: Path, redactor: Redactor, log: RunLog) -> tuple[Surface, Callable[[], None]]:
        from anton.surface.web import PlaywrightSurface

        opts = ReplayOptions(headless=self.opts.headless, record_video=self.opts.record_video)  # the CLI refuses video for masked profiles
        pw, browser, context = _launch(opts, run_dir)
        page = context.new_page()
        surface = PlaywrightSurface(page, redactor, self.bundle.profile.sensitive_elements, self.bundle.route_prefix, screenshot_dir=run_dir / "screenshots", on_event=log.event)

        def close() -> None:
            for closer in (context.close, browser.close, pw.stop):
                try:
                    closer()
                except Exception:  # noqa: BLE001, S110
                    pass

        return surface, close

    def _write_digest(self, name: str, snap: SemanticSnapshot) -> Path:
        path = self.run_dir / "digests" / name
        payload = {"path": self.surface.path(), "url": snap.url, "title": snap.title, "viewport": list(self.surface.viewport()), "elements": digest_of(snap), "snapshot": snap.model_dump(mode="json")}
        path.write_text(json.dumps(self.redactor.scrub(payload), indent=1) + "\n", encoding="utf-8")
        return path

    def _settle(self, check: Callable[[], None]) -> SemanticSnapshot:
        """After an action: wait (bounded) for a navigation / the pane; the allowlist check runs BEFORE
        every observe so an off-list page is never enumerated (SPEC §8 layer 2)."""
        deadline = time.monotonic() + 3.0
        check()
        snap = self.surface.observe()
        while time.monotonic() < deadline and (snap.frame_count == 0 or not snap.elements):
            self.opts.sleep(0.25)
            check()
            snap = self.surface.observe()
        self.opts.sleep(0.5)
        check()
        return self.surface.observe()

    # --- the loop -------------------------------------------------------------------------------------------

    def run(self) -> DiscoveryResult:
        o = self.opts
        started = datetime.now(UTC)
        t_start = time.monotonic()
        run_id = self.run_dir.name
        status, reason, failure_kind, blocked, artifact_path = "GAVE_UP", None, None, None, None
        turns_done = 0
        recorded: list[tuple[TurnRecord, SemanticSnapshot, str, Path]] = []
        client = self._client
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.log.event("run", {"run_id": run_id, "mode": "discover", "goal": o.goal, "capability_id": o.spec.capability_id, "model": o.model, "entry_url": self.bundle.entry_url, "params": {p.name: ("***" if p.sensitive else o.params.get(p.name)) for p in o.spec.inputs}, "approve_risky": o.approve_risky, "escalate": o.escalate})
        self.surface, close = (self._surface_factory or self._default_surface)(self.run_dir, self.redactor, self.log)
        engine = ReplayEngine(self.bundle, o.params, ReplayOptions(approve_risky=o.approve_risky, sleep=o.sleep), self.surface, self.gate, self.log, self.redactor, [])
        engine._secret_values |= self.secret_values
        messages: list[dict[str, Any]] = []
        final_snap: SemanticSnapshot | None = None
        try:
            if self._login:
                try:
                    engine._preconditions()
                except _Failed as f:
                    self.log.event("discovery", {"phase": "login_failed", "kind": f.kind.value, "detail": f.observed})
                    return self._result(run_id, o.goal, "FAILED", failure_kind=f.kind, reason=f"login/preconditions: {f.observed}", turns=0, started=started)
                except _Terminal as t:
                    return self._result(run_id, o.goal, "FAILED", failure_kind=FailureKind.CHECKPOINT_MISS, reason=f"login: {t}", turns=0, started=started)
            self.log.event("discovery", {"phase": "authenticated", "path": self.surface.path(), "model": o.model})
            same_action = 0
            no_change = 0
            last_key: tuple[str, str] | None = None
            last_sig: str | None = None
            for turn in range(1, o.max_steps + 1):
                if time.monotonic() - t_start > o.wall_clock_s:
                    reason = f"wall clock {o.wall_clock_s}s exceeded"
                    break
                snap = self.surface.observe()
                if last_sig is None:
                    last_sig = page_signature(snap, self.surface.path())
                before_path = self.surface.path()
                digest_path = self._write_digest(f"turn-{turn:02d}.json", snap)
                self.log.observation(f"turn{turn}", snap)
                shot = self.surface.screenshot(self.bundle.profile.sensitive_elements)
                png_b64 = base64.standard_b64encode(Path(shot).read_bytes()).decode("ascii")
                for m in messages:  # short history: older digests/screenshots collapse to a stub
                    if m["role"] == "user" and isinstance(m["content"], list):
                        m["content"] = [c for c in m["content"] if c.get("type") != "image"]
                        for c in m["content"]:
                            if c.get("type") == "text" and c["text"].startswith("PAGE DIGEST"):
                                c["text"] = "PAGE DIGEST (older turn, omitted)"
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": f"PAGE DIGEST turn {turn} — url path {before_path!r}, title {snap.title!r}:\n" + json.dumps(digest_of(snap))},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
                ]})
                self._append(self.transcript_path, {"turn": turn, "request": {"model": o.model, "system": self._system(), "messages": _serialize_messages(messages)}})
                response = client.messages.create(model=o.model, max_tokens=4000, system=self._system(), tools=TOOLS, messages=messages, thinking={"type": "adaptive"})
                usage = response.usage.model_dump() if getattr(response, "usage", None) is not None and hasattr(response.usage, "model_dump") else None
                self._append(self.transcript_path, {"turn": turn, "response": {"stop_reason": getattr(response, "stop_reason", None), "content": [_dump_block(b) for b in response.content], "usage": usage}})
                messages.append({"role": "assistant", "content": response.content})
                turns_done = turn
                text = " ".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text").strip()
                tool_blocks = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
                if not tool_blocks:
                    reason = f"model stopped without a tool call: {text[:200]}"
                    self.log.event("discovery", {"phase": "stopped_without_tool", "turn": turn, "text": text[:300]})
                    break
                tool = tool_blocks[0]
                if len(tool_blocks) > 1:
                    self.log.event("discovery", {"phase": "extra_tool_calls_ignored", "turn": turn, "count": len(tool_blocks) - 1})
                args = dict(tool.input)
                rationale = str(args.pop("rationale", "") or text)[:500]
                self.log.event("rationale", {"turn": turn, "tool": tool.name, "text": rationale})
                rec = TurnRecord(turn=turn, digest_path=str(digest_path.relative_to(self.run_dir)), screenshot_path=self.log.relative(Path(shot)), rationale=rationale, tool=tool.name, args={}, gate_decision="n/a", page_signature_after="")
                result_text = ""
                stop = False
                if tool.name == "read":
                    ref = str(args.get("ref"))
                    el = next((e for e in snap.elements if e.ref == ref), None)
                    rec.args = {"ref": ref}
                    if el is None:
                        result_text = f"unknown ref {ref}; use a ref from the current digest"
                    else:
                        rec.executed = describe(snap, el)
                        result_text = f"text of {ref}: {el.text!r}"
                        self.read_values.append(el.text)
                    rec.page_signature_after = last_sig or page_signature(snap, before_path)
                elif tool.name == "act":
                    ref = args.get("ref")
                    try:
                        action = ActionType(str(args.get("action")))
                    except ValueError:
                        action = None
                    value = args.get("value")
                    rec.args = {"ref": ref, "action": str(args.get("action")), "value": value}
                    if action is None or action is ActionType.READ:
                        result_text = f"unsupported action {args.get('action')!r}"
                        rec.gate_decision = "not_attempted"
                    else:
                        rec.action = action
                        rec.value_ref = value_ref(value)
                        el = next((e for e in snap.elements if e.ref == ref), None) if ref else None
                        target = self.surface.resolve_ref(str(ref)) if ref else None
                        if ref and target is None:
                            result_text = f"unknown or stale ref {ref}; use a ref from the current digest"
                            rec.gate_decision = "not_attempted"
                        elif action in (ActionType.CLICK, ActionType.FILL, ActionType.SELECT) and target is None:
                            result_text = f"{action.value} needs a ref"
                            rec.gate_decision = "not_attempted"
                        else:
                            concrete_value, kind = None, None
                            vr = rec.value_ref
                            try:
                                if isinstance(vr, ParamRef):
                                    if vr.name not in o.params:
                                        raise KeyError(vr.name)
                                    concrete_value, kind = o.params[vr.name], "param"
                                elif isinstance(vr, SecretRef):
                                    concrete_value, kind = resolve_secret(vr.name), "secret"
                                    self.secret_values.add(concrete_value)
                                    self.redactor.add_sensitive_value(concrete_value)
                                elif isinstance(vr, LiteralValue):
                                    concrete_value, kind = vr.value, "literal"
                            except KeyError as e:
                                result_text = f"unknown param/secret {e}"
                                rec.gate_decision = "not_attempted"
                            if rec.gate_decision != "not_attempted":
                                concrete = ConcreteAction(action, target=target, value=concrete_value, url=concrete_value if action is ActionType.NAVIGATE else None, value_kind=kind)  # type: ignore[arg-type]
                                engine._secret_values |= self.secret_values
                                decision = engine._gate(concrete, f"turn{turn}", RiskClass.SAFE)
                                if isinstance(decision, Allow):
                                    rec.gate_decision = f"Allow:{decision.risk.value}"
                                    if decision.approval_by:
                                        self.log.event("approval_used", {"step_id": f"turn{turn}", "by": decision.approval_by, "rule": decision.rule})
                                    if el is not None:
                                        rec.executed = describe(snap, el)
                                    engine._log_action(f"turn{turn}", concrete)
                                    try:
                                        self.surface.act(concrete, ACT_TIMEOUT_MS)
                                        result_text = f"{action.value} executed"
                                    except Exception as e:  # noqa: BLE001 - reported to the model
                                        result_text = f"{action.value} failed: {str(e)[:200]}"
                                        rec.gate_decision += ";act_failed"  # gated, but never executed: not a step (distiller)
                                elif isinstance(decision, NeedsApproval):
                                    rec.gate_decision = "NeedsApproval"
                                    rec.page_signature_after = last_sig or ""
                                    self._append(self.turns_path, rec.model_dump(mode="json"))
                                    blocked = BlockedDetail(kind=BlockedKind.APPROVAL_REQUIRED, step_id=f"turn{turn}", lease_state="AGENT_ACTIVE")
                                    return self._result(run_id, o.goal, "BLOCKED", blocked=blocked, reason=f"RISKY action at turn {turn} without --approve-risky: {decision.detail}", turns=turn, started=started)
                                else:
                                    assert isinstance(decision, Deny)
                                    rec.gate_decision = f"Deny:{decision.reason.value}"
                                    result_text = f"denied by policy: {decision.reason.value}: {decision.detail}"
                    # Layer 2 (§8): a navigation off the allowlist stops the run before the page is observed.
                    try:
                        step_label = f"turn{turn}"
                        after = self._settle(lambda lbl=step_label: engine._check_navigation(engine.steps_out[-1] if engine.steps_out else _placeholder(), None, lbl))
                    except _Failed as f:
                        rec.page_signature_after = "off-allowlist"
                        self._append(self.turns_path, rec.model_dump(mode="json"))
                        return self._result(run_id, o.goal, "FAILED", failure_kind=FailureKind.POLICY_VIOLATION if f.deny_reason is DenyReason.POST_NAV_OFFLIST else f.kind, reason=f.observed, turns=turn, started=started)
                    sig = page_signature(after, self.surface.path())
                    rec.page_signature_after = sig
                    key = (str(action.value if action else "?"), json.dumps(rec.executed.model_dump(mode="json"), sort_keys=True) if rec.executed else str(ref))
                    if key == last_key and sig == last_sig:
                        same_action += 1
                    else:
                        same_action = 1
                    no_change = no_change + 1 if sig == last_sig else 0
                    last_key, last_sig = key, sig
                    if same_action >= 3 or no_change >= 6:
                        self._append(self.turns_path, rec.model_dump(mode="json"))
                        reason = "dead-end: " + ("3 identical actions with an unchanged page" if same_action >= 3 else "6 actions without a page change")
                        self.log.event("discovery", {"phase": "dead_end", "turn": turn, "reason": reason})
                        break
                elif tool.name == "finish":
                    outputs = args.get("outputs") or {}
                    rec.args = {"outputs": {str(k): str(v) for k, v in dict(outputs).items()}}
                    rec.page_signature_after = last_sig or page_signature(snap, before_path)
                    self._append(self.turns_path, rec.model_dump(mode="json"))
                    recorded.append((rec, snap, before_path, digest_path))
                    final_snap = snap
                    stop = True
                elif tool.name in ("give_up", "ask_human"):
                    why = str(args.get("reason", ""))[:300]
                    rec.args = {"reason": why}
                    rec.page_signature_after = last_sig or page_signature(snap, before_path)
                    self._append(self.turns_path, rec.model_dump(mode="json"))
                    reason = f"{tool.name}: {why}" + (" (no human available; mapped to give_up)" if tool.name == "ask_human" and not o.escalate else "")
                    self.log.event("discovery", {"phase": tool.name, "turn": turn, "reason": why})
                    break
                else:
                    result_text = f"unknown tool {tool.name}"
                    rec.page_signature_after = last_sig or ""
                if stop:
                    break
                self._append(self.turns_path, rec.model_dump(mode="json"))
                recorded.append((rec, snap, before_path, digest_path))
                results = [{"type": "tool_result", "tool_use_id": tool.id, "content": self.redactor.scrub_text(result_text)}]
                # One tool call per turn: every extra tool_use block still needs a tool_result (API contract).
                results += [{"type": "tool_result", "tool_use_id": extra.id, "content": "ignored: exactly one tool call per turn; re-observe and call again", "is_error": True} for extra in tool_blocks[1:]]
                messages.append({"role": "user", "content": results})
            else:
                reason = f"max_steps {o.max_steps} reached"
            if final_snap is not None:
                status, reason, artifact_path = self._finish(recorded, run_id)
        except _Failed as f:
            status, failure_kind, reason = "FAILED", f.kind, f.observed
        except Exception as e:  # noqa: BLE001 - a crash is a FAILED result with the error logged, never a traceback
            self.log.event("engine_error", {"type": type(e).__name__, "detail": str(e)[:300]})
            status, failure_kind, reason = "FAILED", FailureKind.CHECKPOINT_MISS, f"engine error: {type(e).__name__}: {str(e)[:200]}"
        finally:
            close()
        return self._result(run_id, o.goal, status, reason=reason, artifact_path=artifact_path, turns=turns_done, started=started, failure_kind=failure_kind, blocked=blocked)

    # --- finish → distill -------------------------------------------------------------------------------------

    def _finish(self, recorded: list[tuple[TurnRecord, SemanticSnapshot, str, Path]], run_id: str) -> tuple[str, str | None, str | None]:
        o = self.opts
        finish_rec, finish_snap, _, _ = recorded[-1]
        cited = finish_rec.args.get("outputs") or {}
        assert isinstance(cited, dict)
        values: dict[str, str | None] = {}
        for decl in o.spec.outputs:
            ref = cited.get(decl.name)
            live = self.surface.resolve_ref(str(ref)) if ref else None
            raw = self.surface.read(live, "value" if live is not None and live.role in ("textbox", "combobox") else "text") if live is not None else None
            values[decl.name] = raw
            if raw and decl.sensitive:
                self.redactor.add_sensitive_value(raw)
            if raw:
                self.read_values.append(raw)
        self.log.event("discovery", {"phase": "finish", "outputs": {k: ("***" if any(d.name == k and d.sensitive for d in o.spec.outputs) else v) for k, v in values.items()}})
        missing = [k for k, v in values.items() if not v]
        if missing:
            return "GAVE_UP", f"finish cited refs whose text is empty/unresolved for outputs {missing}", None
        turns: list[RecordedTurn] = []
        for i, (rec, snap, path, _) in enumerate(recorded):
            nxt = recorded[i + 1] if i + 1 < len(recorded) else None
            turns.append(RecordedTurn(record=rec, before=snap, after=nxt[1] if nxt else None, before_path=path, after_path=nxt[2] if nxt else None, viewport=self.surface.viewport()))
        ctx = DistillContext(spec=o.spec, profile=self.bundle.profile, profile_path=o.profile_path, params=o.params, entry_url=self.bundle.entry_url, route_prefix=self.bundle.route_prefix,
                             run_id=run_id, model=o.model, secret_values=set(self.secret_values), read_values=list(self.read_values))
        sensitive = {p.name for p in o.spec.inputs if p.sensitive}
        meta = {"run_id": run_id, "goal": o.goal, "capability_id": o.spec.capability_id, "params": {k: ("***" if k in sensitive else v) for k, v in o.params.items()},
                "entry_url": self.bundle.entry_url, "route_prefix": self.bundle.route_prefix, "model": o.model, "read_values": list(self.read_values),
                "human_takeover": False, "profile_path": o.profile_path}
        self.log.write_json("discovery.json", meta)  # through the Redactor like every other side file
        try:
            artifact = distill(turns, ctx)
        except DistillError as e:
            self.log.event("discovery", {"phase": "distill_failed", "detail": str(e)})
            return "GAVE_UP", f"distill: {e}", None
        out_dir = Path(o.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{o.spec.capability_id.split('.')[-1]}.json"
        text = artifact.model_dump_json(indent=2) + "\n"
        if self.redactor.scrub_text(text) != text:
            self.log.event("discovery", {"phase": "distill_failed", "detail": "the distilled artifact would contain a sensitive value"})
            return "GAVE_UP", "distill: the artifact would contain a sensitive value (secret or registered output)", None
        path.write_text(text, encoding="utf-8")
        shown = _display_path(path)
        try:
            load_bundle(path, policy=self.bundle.policy)  # every §4 loader rule must accept the draft
        except Exception as e:  # noqa: BLE001 - LoaderError or validation
            self.log.event("discovery", {"phase": "distill_invalid", "detail": str(e)[:300]})
            return "GAVE_UP", f"distilled artifact fails the loader: {e}", None
        self.log.event("discovery", {"phase": "distilled", "artifact": shown, "steps": [s.id for s in artifact.steps]})
        return "SUCCESS", None, shown

    def _result(self, run_id: str, goal: str, status: str, *, reason: str | None = None, artifact_path: str | None = None, turns: int = 0,
                started: datetime, failure_kind: FailureKind | None = None, blocked: BlockedDetail | None = None) -> DiscoveryResult:
        res = DiscoveryResult(run_id=run_id, goal=goal, status=status, failure_kind=failure_kind if status == "FAILED" else None, blocked=blocked if status == "BLOCKED" else None,
                              artifact_path=artifact_path if status == "SUCCESS" else None, turns=turns, reason=reason, started_at=started, finished_at=datetime.now(UTC))
        self.log.write_json("result.json", res)
        self.log.event("result", {"status": status, "reason": reason, "artifact_path": artifact_path})
        self.log.close()
        return res


def _display_path(path: Path) -> str:
    """Repo-root-relative when inside the repo (results and logs never carry a machine-specific path)."""
    try:
        return str(path.resolve().relative_to(repo_root().resolve()))
    except ValueError:
        return str(path)


def _placeholder():  # noqa: ANN202
    from anton.schema.result import StepResult, StepStatus

    return StepResult(step_id="discovery", status=StepStatus.PASSED, attempts=1, duration_ms=0)


def run_discovery(opts: DiscoverOptions, profile: AppProfile, policy: PolicyConfig, url: str | None, *, run_id: str | None = None, runs_root: Path | None = None) -> DiscoveryResult:
    bundle = discovery_bundle(profile, policy, url, opts.profile_path)
    run_id = run_id or new_run_id()
    run_dir = (runs_root or repo_root() / "runs") / run_id
    run_dir.mkdir(parents=True)
    return Discovery(opts, bundle, run_dir).run()


_ = re  # keep the regex module import explicit for the digest helpers above
