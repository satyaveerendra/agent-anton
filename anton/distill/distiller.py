"""SPEC §10.3 — deterministic distillation of a recorded discovery run into a draft artifact.

Input: the run's ``TurnRecord``s with their before/after snapshots (the "after" snapshot of a
turn is the next turn's pre-action snapshot), the declared ``CapabilitySpec``, the ``AppProfile``
and the param VALUES the run used. Output: ``status: draft`` ``CapabilityArtifact``. No model call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from anton.discovery.describe import describe
from anton.schema.app_profile import AppProfile
from anton.schema.artifact import (
    ActionType,
    BBoxLocator,
    CapabilityArtifact,
    Checked,
    ElementFingerprint,
    ElementVisible,
    ExactTextLocator,
    ExtractionSpec,
    FieldValue,
    LabelRelativeLocator,
    LiteralValue,
    LocatorCandidate,
    LocatorLadder,
    LocatorTier,
    OutputSpec,
    OutputType,
    ParamRef,
    Predicate,
    Provenance,
    RecordedAgainst,
    RiskClass,
    RoleNameLocator,
    SecretRef,
    StateSignature,
    StepSpec,
    TextVisible,
    TitleMatches,
    UrlMatches,
    ValueRef,
)
from anton.schema.snapshot import ElementDescriptor, SemanticSnapshot, TurnRecord
from anton.schema.spec import CapabilitySpec

__all__ = ["DistillError", "RecordedTurn", "distill", "bind_tokens", "bind_value", "canonical_path_regex", "step_id_for", "ladder_for", "landmark_text", "title_landmark", "slug", "regex_literal", "relative_url", "load_recorded_run", "redistill"]

def regex_literal(text: str) -> str:
    """Escape only regex metacharacters (``re.escape`` also escapes spaces, which reads badly in a
    reviewed artifact); whole-token param binding happens on the result."""
    return re.sub(r"([.^$*+?{}\[\]\\|()])", r"\\\1", text)


MONEY_RX = re.compile(r"^\s*[-(]?\s*\$\s*[\d,]+(\.\d+)?\s*\)?\s*$")
LANDMARK_ROLES = ("heading", "text")


class DistillError(Exception):
    pass


@dataclass
class RecordedTurn:
    record: TurnRecord
    before: SemanticSnapshot
    #: The page after the action = the next turn's pre-action snapshot (None for the last turn).
    after: SemanticSnapshot | None
    before_path: str
    after_path: str | None
    viewport: tuple[int, int]


# --- binding (§10.3 rule 1) ------------------------------------------------------------------------


def _norm_number(s: str) -> str | None:
    t = re.sub(r"[$,\s]", "", s)
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        return str(float(t))
    return None


def bind_value(value: str, params: dict[str, str]) -> ValueRef:
    """A typed/selected value binds to a param on exact match, or numeric equality after normalize."""
    for name, pv in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if value == pv:
            return ParamRef(name=name)
        nv, np_ = _norm_number(value), _norm_number(pv)
        if nv is not None and np_ is not None and nv == np_:
            return ParamRef(name=name)
    return LiteralValue(value=value)


def bind_tokens(text: str, params: dict[str, str]) -> str:
    """Locator strings / snippets / regex sources: a param value binds on a WORD-BOUNDARY match —
    ``Member (204817)`` and ``204817,`` bind, ``204817x`` and ``Holiday funds`` do not — except inside a
    number/currency token: ``$50.00`` never binds to ``50`` (no ``$``/``.`` before, no ``.digit`` after).
    Longest values first so a value that contains another binds as itself."""
    if not text:
        return text
    out = text
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if value:
            out = re.sub(r"(?<![\w$.])" + re.escape(value) + r"(?!\w|\.\d)", f"$param:{name}", out)
    return out


def relative_url(url: str) -> str:
    """Artifacts carry origin-relative URLs (SPEC §8): strip scheme + host, keep path/query."""
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))
    return url


def canonical_path_regex(path: str, params: dict[str, str], route_prefix: str = "") -> str:
    """``/portal/member/204817`` → ``^/member/[^/]+$`` (prefix stripped, param-valued and numeric
    segments generalized, everything else escaped)."""
    p = urlsplit(path).path or "/"
    if route_prefix and (p == route_prefix or p.startswith(route_prefix + "/")):
        p = p[len(route_prefix):] or "/"
    values = {v for v in params.values() if v}
    segs = []
    for seg in p.split("/"):
        if seg == "":
            segs.append("")
        elif seg in values or re.fullmatch(r"\d+", seg):
            segs.append("[^/]+")
        else:
            segs.append(re.escape(seg))
    return "^" + "/".join(segs) + "$"


# --- ids (§10.3 rule 10) -------------------------------------------------------------------------------


def slug(text: str, words: int = 4) -> str:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(toks[:words]) or "element"


def step_id_for(action: ActionType, name: str | None, used: set[str], *, path: str | None = None, key: str | None = None, params: dict[str, str] | None = None) -> str:
    if action is ActionType.NAVIGATE:
        # the slug of the CANONICAL path (param-valued / numeric segments dropped), so ids are stable across params
        canon = canonical_path_regex(path or "/", params or {}).strip("^$").replace("[^/]+", "").replace("\\", "")
        base = "navigate_" + slug(re.sub(r"[/]+", " ", canon) or "root")
    elif action is ActionType.PRESS and not name:
        base = "press_" + slug(key or "key")
    else:
        base = f"{action.value.lower()}_{slug(name or '')}"
    if not re.match(r"^[a-z]", base):
        base = "s_" + base
    sid, n = base, 1
    while sid in used:
        n += 1
        sid = f"{base}_{n}"
    used.add(sid)
    return sid


# --- ladders (§10.3 rule 2) ------------------------------------------------------------------------------


def ladder_for(desc: ElementDescriptor, viewport: tuple[int, int], params: dict[str, str], *, extraction: bool = False) -> LocatorLadder:
    frame = list(desc.frame_path)
    cands: list[LocatorCandidate] = []
    name = bind_tokens(desc.name, params)
    if not extraction and desc.name.strip():
        cands.append(RoleNameLocator(tier=LocatorTier.SEMANTIC, role=desc.role, name=name, frame_path=frame, rationale=f"computed role {desc.role!r} + accessible name {desc.name!r} observed during discovery; the semantic identity of the control"))
    if desc.row_anchor and desc.col_header:
        cands.append(LabelRelativeLocator(
            tier=LocatorTier.ANCHORED, relation="row_cell", anchor_text=bind_tokens(desc.row_anchor, params),
            anchor_col_header=bind_tokens(desc.row_anchor_col_header, params) if desc.row_anchor_col_header else None, col_header=bind_tokens(desc.col_header, params), frame_path=frame,
            rationale=f"the cell at (row anchored by {desc.row_anchor!r} in column {desc.row_anchor_col_header!r}) × (column headed {desc.col_header!r}); survives column reordering",
        ))
    elif desc.label_context and desc.label_context.strip() and desc.label_context != desc.text:
        target_role = desc.role if desc.role in ("textbox", "combobox", "checkbox", "radio", "button", "link", "cell", "text") else None
        cands.append(LabelRelativeLocator(
            tier=LocatorTier.ANCHORED, relation="right_of_label", anchor_text=bind_tokens(desc.label_context, params), target_role=target_role, frame_path=frame,
            rationale=f"the element sits right of its label {desc.label_context!r}; survives a name/attribute change",
        ))
    elif not extraction and desc.text.strip() and desc.text != desc.name:
        cands.append(ExactTextLocator(tier=LocatorTier.ANCHORED, text=bind_tokens(desc.text, params), frame_path=frame, rationale="the visible text is the label users see"))
    vw, vh = viewport
    b = desc.bbox
    x, y = min(max(b.x / vw, 0.0), 1.0), min(max(b.y / vh, 0.0), 1.0)
    w, h = min(max(b.w / vw, 1e-4), 1.0 - x) or 1e-4, min(max(b.h / vh, 1e-4), 1.0 - y) or 1e-4
    cands.append(BBoxLocator(tier=LocatorTier.VISUAL, x=round(x, 4), y=round(y, 4), w=round(max(w, 1e-4), 4), h=round(max(h, 1e-4), 4), frame_path=frame, rationale="normalized box observed during discovery; last resort, sets drift_flag"))
    if extraction:
        fp = ElementFingerprint(role=desc.role, control_type=desc.control_type)  # never the value as a snippet
    else:
        fp = ElementFingerprint(role=desc.role, control_type=desc.control_type, name_snippet=name if desc.name.strip() else None)
    return LocatorLadder(candidates=cands, fingerprint=fp)


# --- landmark rule (§10.3 rule 3) --------------------------------------------------------------------------


TITLE_SEPARATORS = re.compile(r"\s+[—–|:-]\s+")


def title_landmark(before: SemanticSnapshot, after: SemanticSnapshot, exclude: list[str]) -> str | None:
    """Fallback landmark when no static text qualifies: the most specific segment of the document
    title (split on ' — ' / ' - ' / ' | ') that is new after the action and free of param/read values."""
    if after.title == before.title or not after.title.strip():
        return None
    old_segments = {seg.strip() for seg in TITLE_SEPARATORS.split(before.title)}
    for seg in TITLE_SEPARATORS.split(after.title):
        seg = seg.strip()
        if seg and seg not in old_segments and "***" not in seg and not any(x and x in seg for x in exclude):
            return seg
    return None


def landmark_text(before: SemanticSnapshot, after: SemanticSnapshot, exclude: list[str]) -> str | None:
    """The most salient static heading/text present after the action and absent before, excluding
    any text that contains a param value or a read value."""
    seen = {e.text.strip() for e in before.elements if e.visible}
    cands = [e for e in after.elements if e.visible and e.role in LANDMARK_ROLES and e.text.strip() and e.text.strip() not in seen and "***" not in e.text]
    cands = [e for e in cands if not any(x and x in e.text for x in exclude)]
    if not cands:
        return None
    cands.sort(key=lambda e: (0 if e.role == "heading" else 1, 0 if not e.frame_path else 1, e.bbox.y, e.bbox.x))
    return cands[0].text.strip()


# --- the distiller -------------------------------------------------------------------------------------------


@dataclass
class DistillContext:
    spec: CapabilitySpec
    profile: AppProfile
    #: Repo-root-relative path written into ``CapabilityArtifact.app_profile``.
    profile_path: str
    params: dict[str, str]
    entry_url: str
    route_prefix: str
    run_id: str
    model: str | None
    secret_values: set[str] = field(default_factory=set)
    read_values: list[str] = field(default_factory=list)
    human_takeover: bool = False
    wait_timeout_ms: int = 8000


def _bind_value_ref(vr: ValueRef | None, ctx: DistillContext) -> ValueRef | None:
    if vr is None:
        return None
    if isinstance(vr, SecretRef):
        return vr
    if isinstance(vr, ParamRef):
        return vr
    if vr.value in ctx.secret_values:
        raise DistillError("a typed literal equals a resolved secret value; the run must use $secret:<name>")
    return bind_value(vr.value, ctx.params)


def _run_timestamp(run_id: str) -> datetime:
    """``created_at`` is derived from the run id (``YYYYMMDDTHHMMSSZ-xxx``) so the same run always
    distills to a byte-identical artifact (same content hash); an unknown id shape falls back to now."""
    m = re.match(r"^(\d{8}T\d{6})Z", run_id)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    return datetime.now(UTC)


def _login_landmark(profile: AppProfile) -> LocatorLadder:
    last = profile.flows["login"][-1].postcondition
    return next(p.ladder for p in last.all_of if isinstance(p, ElementVisible))


def distill(turns: list[RecordedTurn], ctx: DistillContext) -> CapabilityArtifact:
    params = ctx.params
    exclude = [v for v in params.values() if v] + list(ctx.read_values)
    used_ids: set[str] = set()
    steps: list[StepSpec] = []
    finish: RecordedTurn | None = None
    for t in turns:
        r = t.record
        if r.tool == "read":
            continue  # READ turns are never steps (rule 7)
        if r.tool == "finish":
            finish = t
            break
        if r.tool != "act" or r.action is None or r.gate_decision.split(";")[0] not in ("Allow:SAFE", "Allow:RISKY") or ";act_failed" in r.gate_decision:
            continue  # denied / not attempted / failed-to-execute actions never became steps
        if t.after is None:
            raise DistillError(f"turn {r.turn}: no observation after the action")
        action = r.action
        desc = r.executed
        target = ladder_for(desc, t.viewport, params) if desc is not None else None
        value = _bind_value_ref(r.value_ref, ctx)
        if action is ActionType.NAVIGATE and isinstance(value, LiteralValue):
            value = LiteralValue(value=relative_url(value.value))  # origin-relative (SPEC §8); path params stay literal (§4: tokens live in locators/regexes)
        risk = RiskClass.RISKY if "RISKY" in r.gate_decision else RiskClass.SAFE
        preds: list[Predicate] = []
        before_path, after_path = t.before_path, t.after_path or t.before_path
        if after_path != before_path or action is ActionType.NAVIGATE:
            preds.append(UrlMatches(regex=canonical_path_regex(after_path, params, ctx.route_prefix)))
        lm = landmark_text(t.before, t.after, exclude)
        if lm is not None:
            preds.append(TextVisible(regex=bind_tokens(regex_literal(lm), params)))
        else:
            tl = title_landmark(t.before, t.after, exclude)
            if tl is not None:
                preds.append(TitleMatches(regex=regex_literal(tl)))
        if target is not None and action in (ActionType.FILL, ActionType.SELECT) and value is not None:
            preds.append(FieldValue(ladder=target, expected=value))
        if target is not None and action is ActionType.CLICK and desc is not None and desc.role in ("checkbox", "radio"):
            after_el = next((e for e in t.after.elements if e.frame_path == desc.frame_path and e.role == desc.role and e.name == desc.name), None)
            preds.append(Checked(ladder=target, expected=bool(after_el.checked) if after_el is not None else True))
        if not preds:
            preds.append(UrlMatches(regex=canonical_path_regex(after_path, params, ctx.route_prefix)))
        sid = step_id_for(action, desc.name if desc else None, used_ids, path=str(r.args.get("value")) if action is ActionType.NAVIGATE else None, key=str(r.args.get("value")) if action is ActionType.PRESS else None, params=params)
        steps.append(StepSpec(
            id=sid, description=bind_tokens(r.rationale[:200], params) or f"{action.value} {desc.name if desc else ''}".strip(), action=action, target=target,
            value=value, risk=risk, postcondition=StateSignature(all_of=preds), wait_timeout_ms=ctx.wait_timeout_ms, retries=0 if risk is RiskClass.RISKY else 1,
        ))
    if finish is None:
        raise DistillError("the run did not finish; nothing to distill")
    if not steps:
        raise DistillError("the run contains no executed actions")
    if ctx.human_takeover:
        # §10.3: a run that contained a human TAKEOVER is always draft; the description says why (the
        # human's actions are evidence, never steps — REPORT §7).
        takeover_note = " [draft: a human took over during discovery; review the recorded human actions before approving]"
    else:
        takeover_note = ""
    outputs_cited = finish.record.args.get("outputs") or {}
    if not isinstance(outputs_cited, dict):
        raise DistillError("finish did not carry {name: ref}")
    outputs: list[OutputSpec] = []
    for decl in ctx.spec.outputs:
        ref = outputs_cited.get(decl.name)
        if ref is None:
            raise DistillError(f"finish did not cite output {decl.name!r}")
        el = next((e for e in finish.before.elements if e.ref == ref), None)
        if el is None:
            raise DistillError(f"finish cited unknown ref {ref!r} for {decl.name!r}")
        desc = describe(finish.before, el)
        ladder = ladder_for(desc, finish.viewport, params, extraction=True)
        attr = "value" if el.role in ("textbox", "combobox") else "text"
        text = el.value if attr == "value" and el.value is not None else el.text
        if MONEY_RX.match(text or "") or decl.type is OutputType.NUMBER and MONEY_RX.match(text or ""):
            normalize = "money"
        elif decl.type is OutputType.INTEGER:
            normalize = "digits"
        elif decl.type is OutputType.NUMBER:
            normalize = "money"
        else:
            normalize = "trim"
        outputs.append(decl.to_output_spec(ExtractionSpec(locator=ladder, attr=attr, normalize=normalize)))
    codes = dict(ctx.spec.business_outcomes)
    for d in ctx.profile.global_detectors:
        if d.arm == "BUSINESS_OUTCOME" and d.outcome_code not in codes:
            codes[d.outcome_code] = f"declared by app detector {d.id!r}"
    entry_path = urlsplit(ctx.entry_url).path or "/"
    preconditions = StateSignature(all_of=[UrlMatches(regex=canonical_path_regex(entry_path, params, ctx.route_prefix)), ElementVisible(ladder=_login_landmark(ctx.profile))])
    success_preds: list[Predicate] = list(steps[-1].postcondition.all_of) + [ElementVisible(ladder=o.extraction.locator) for o in outputs]
    art = CapabilityArtifact(
        capability_id=ctx.spec.capability_id, name=ctx.spec.name, description=ctx.spec.description + takeover_note, version="1.0.0", status="draft",
        app_profile=ctx.profile_path, requires_flows=["login"], preconditions=preconditions, inputs=list(ctx.spec.inputs),
        outputs=outputs, business_outcomes=codes, steps=steps, success=StateSignature(all_of=success_preds),
        provenance=Provenance(origin="discovery", discovery_run_id=ctx.run_id, model=ctx.model, created_at=_run_timestamp(ctx.run_id),
                              recorded_against=RecordedAgainst(app_id=ctx.profile.app_id, tenant_id=ctx.profile.tenant_id, app_version=ctx.profile.app_version, entry_url=ctx.entry_url)),
    )
    return art


# --- re-distillation from a run directory --------------------------------------------------------------------


def load_recorded_run(run_dir: Path) -> tuple[list[RecordedTurn], dict]:
    """Rebuild the ``RecordedTurn``s from ``turns.jsonl`` + ``digests/`` and read ``discovery.json``
    (params, entry URL, prefix, model, read/secret bookkeeping) — the distiller's exact inputs."""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "discovery.json").read_text(encoding="utf-8"))
    records = [TurnRecord.model_validate_json(line) for line in (run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    digests = []
    for r in records:
        d = json.loads((run_dir / r.digest_path).read_text(encoding="utf-8"))
        digests.append((SemanticSnapshot.model_validate(d["snapshot"]), d["path"], tuple(d["viewport"])))
    turns: list[RecordedTurn] = []
    for i, r in enumerate(records):
        snap, path, viewport = digests[i]
        nxt = digests[i + 1] if i + 1 < len(digests) else None
        turns.append(RecordedTurn(record=r, before=snap, after=nxt[0] if nxt else None, before_path=path, after_path=nxt[1] if nxt else None, viewport=viewport))
    return turns, meta


def redistill(run_dir: Path, spec: CapabilitySpec, profile: AppProfile, profile_path: str) -> CapabilityArtifact:
    """The same run always distills to the same contract (provenance keeps the original run id)."""
    turns, meta = load_recorded_run(run_dir)
    ctx = DistillContext(spec=spec, profile=profile, profile_path=profile_path, params=dict(meta["params"]), entry_url=meta["entry_url"], route_prefix=meta.get("route_prefix", ""),
                         run_id=meta["run_id"], model=meta.get("model"), read_values=list(meta.get("read_values", [])), human_takeover=bool(meta.get("human_takeover", False)))
    return distill(turns, ctx)
