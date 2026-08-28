"""Ladder walk (SPEC §7, §9): surface-independent locator resolution over snapshot geometry.

Tier 1 = role + accessible name (frame-aware). Tier 2 = relations computed over snapshot
geometry ONLY, comparing elements that share the candidate's frame_path — ``right_of_label``,
``below_label``, ``row_cell`` (the cell at the intersection of the row containing the anchor
text and the column whose header matches ``col_header``, which is what survives a column
shift) — never DOM parent/sibling relations, so a desktop adapter can implement them from any
element list with boxes. Tier 3 = normalized bbox → element at point.

The walk expands ``$param:`` tokens first, then tries candidates in order, checking each
resolved element against ``ElementFingerprint`` with the per-tier strictness of SPEC §4:
tiers 1–2 compare role + control_type + name_snippet (+ text_snippet when set); tier 3
compares role + control_type only, so it genuinely survives label drift and sets drift_flag.
``resume_point()`` (SPEC §9/§11) lands with the engine in phase 4.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from anton.schema.artifact import (
    PARAM_TOKEN,
    BBoxLocator,
    ElementFingerprint,
    ExactTextLocator,
    LabelRelativeLocator,
    LocatorCandidate,
    LocatorLadder,
    LocatorTier,
    RoleNameLocator,
)
from anton.schema.result import LocatorResolution
from anton.schema.snapshot import BBox, SemanticSnapshot, SnapshotElement
from anton.surface.base import ResolvedElement, Surface

__all__ = [
    "expand_text",
    "expand_candidate",
    "expand_ladder",
    "expand_fingerprint",
    "norm",
    "resolve_candidate",
    "fingerprint_ok",
    "WalkResult",
    "walk_ladder",
    "resolve_ladder_until",
]

INTERACTIVE_ROLES = frozenset({"button", "link", "textbox", "checkbox", "radio", "combobox"})
CELL_ROLES = frozenset({"cell", "columnheader"})


# --- $param: expansion -------------------------------------------------------------------------


def expand_text(text: str, params: Mapping[str, str]) -> str:
    """Replace ``$param:<name>`` with the caller's ORIGINAL param string (SPEC §4)."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"$param:{name} is not among the provided params {sorted(params)}")
        return params[name]

    return PARAM_TOKEN.sub(sub, text)


def expand_candidate(c: LocatorCandidate, params: Mapping[str, str]) -> LocatorCandidate:
    if isinstance(c, RoleNameLocator):
        return c.model_copy(update={"name": expand_text(c.name, params)})
    if isinstance(c, LabelRelativeLocator):
        return c.model_copy(
            update={
                "anchor_text": expand_text(c.anchor_text, params),
                "anchor_col_header": expand_text(c.anchor_col_header, params) if c.anchor_col_header else None,
                "col_header": expand_text(c.col_header, params) if c.col_header else None,
            }
        )
    if isinstance(c, ExactTextLocator):
        return c.model_copy(update={"text": expand_text(c.text, params)})
    return c


def expand_fingerprint(fp: ElementFingerprint, params: Mapping[str, str]) -> ElementFingerprint:
    return fp.model_copy(
        update={
            "name_snippet": expand_text(fp.name_snippet, params) if fp.name_snippet else None,
            "text_snippet": expand_text(fp.text_snippet, params) if fp.text_snippet else None,
        }
    )


def expand_ladder(ladder: LocatorLadder, params: Mapping[str, str]) -> LocatorLadder:
    return LocatorLadder(
        candidates=[expand_candidate(c, params) for c in ladder.candidates],
        fingerprint=expand_fingerprint(ladder.fingerprint, params),
    )


# --- geometry ------------------------------------------------------------------------------


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


def _same_frame(el: SnapshotElement, frame_path: list[str]) -> bool:
    return el.frame_path == frame_path


def _v_overlap(a: BBox, b: BBox) -> float:
    return max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))


def _h_overlap(a: BBox, b: BBox) -> float:
    return max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))


def _same_row(a: BBox, b: BBox) -> bool:
    return _v_overlap(a, b) >= 0.5 * min(a.h, b.h) if min(a.h, b.h) > 0 else False


def _same_column(a: BBox, b: BBox) -> bool:
    return _h_overlap(a, b) >= 0.5 * min(a.w, b.w) if min(a.w, b.w) > 0 else False


def _text_matches(el: SnapshotElement, wanted: str) -> bool:
    w = norm(wanted)
    return norm(el.text) == w or norm(el.name) == w


def _role_ok(el: SnapshotElement, target_role: str | None, default: frozenset[str] | None) -> bool:
    if target_role is not None:
        return el.role == target_role
    return default is None or el.role in default


def _resolve_role_name(snap: SemanticSnapshot, c: RoleNameLocator) -> SnapshotElement | None:
    wanted = norm(c.name)
    for el in snap.elements:
        if not el.visible or not _same_frame(el, c.frame_path) or el.role != c.role:
            continue
        have = norm(el.name)
        if (have == wanted) if c.exact else (have == wanted or (wanted and wanted in have)):
            return el
    return None


def _anchors(snap: SemanticSnapshot, c: LabelRelativeLocator) -> list[SnapshotElement]:
    """Label anchors are static text/cells — a control whose accessible NAME equals the label
    (the "Password" input) must not anchor itself."""
    return [
        el for el in snap.elements
        if el.visible and _same_frame(el, c.frame_path) and el.role not in INTERACTIVE_ROLES and _text_matches(el, c.anchor_text)
    ]


def _resolve_right_of(snap: SemanticSnapshot, c: LabelRelativeLocator) -> SnapshotElement | None:
    best: tuple[float, SnapshotElement] | None = None
    for a in _anchors(snap, c):
        for el in snap.elements:
            if el is a or not el.visible or not _same_frame(el, c.frame_path):
                continue
            if not _role_ok(el, c.target_role, INTERACTIVE_ROLES | CELL_ROLES | {"text"}):
                continue
            if el.bbox.x < a.bbox.x + a.bbox.w - 1 or not _same_row(el.bbox, a.bbox):
                continue
            d = el.bbox.x - (a.bbox.x + a.bbox.w)
            if best is None or d < best[0]:
                best = (d, el)
        if best is not None:
            return best[1]
    return None


def _resolve_below(snap: SemanticSnapshot, c: LabelRelativeLocator) -> SnapshotElement | None:
    best: tuple[float, SnapshotElement] | None = None
    for a in _anchors(snap, c):
        for el in snap.elements:
            if el is a or not el.visible or not _same_frame(el, c.frame_path):
                continue
            if not _role_ok(el, c.target_role, INTERACTIVE_ROLES | CELL_ROLES | {"text"}):
                continue
            if el.bbox.y < a.bbox.y + a.bbox.h - 1 or not _same_column(el.bbox, a.bbox):
                continue
            d = el.bbox.y - (a.bbox.y + a.bbox.h)
            if best is None or d < best[0]:
                best = (d, el)
        if best is not None:
            return best[1]
    return None


def _resolve_row_cell(snap: SemanticSnapshot, c: LabelRelativeLocator) -> SnapshotElement | None:
    cells = [el for el in snap.elements if el.visible and _same_frame(el, c.frame_path) and el.role in CELL_ROLES]
    headers = [h for h in cells if _text_matches(h, c.col_header or "")]  # col_header is required by the §4 validator
    anchor_headers = [h for h in cells if c.anchor_col_header and _text_matches(h, c.anchor_col_header)]
    for a in cells:
        if not _text_matches(a, c.anchor_text):
            continue
        if c.anchor_col_header and not any(h.bbox.y < a.bbox.y and _same_column(h.bbox, a.bbox) for h in anchor_headers):
            continue
        above = [h for h in headers if h.bbox.y < a.bbox.y]
        if not above:
            continue
        header = max(above, key=lambda h: h.bbox.y)  # the nearest header row above the anchor
        for el in cells:
            if _same_row(el.bbox, a.bbox) and _same_column(el.bbox, header.bbox):
                return el
    return None


def _resolve_exact_text(snap: SemanticSnapshot, c: ExactTextLocator) -> SnapshotElement | None:
    wanted = norm(c.text)
    for el in snap.elements:
        if el.visible and _same_frame(el, c.frame_path) and norm(el.text) == wanted:
            return el
    return None


def _resolve_bbox(snap: SemanticSnapshot, c: BBoxLocator, viewport: tuple[int, int]) -> SnapshotElement | None:
    vw, vh = viewport
    cx, cy = (c.x + c.w / 2) * vw, (c.y + c.h / 2) * vh
    best: tuple[float, SnapshotElement] | None = None
    for el in snap.elements:
        if not el.visible or not _same_frame(el, c.frame_path):
            continue
        b = el.bbox
        if b.x <= cx <= b.x + b.w and b.y <= cy <= b.y + b.h:
            area = b.w * b.h
            if best is None or area < best[0]:
                best = (area, el)
    return best[1] if best else None


def resolve_candidate(snap: SemanticSnapshot, c: LocatorCandidate, viewport: tuple[int, int]) -> SnapshotElement | None:
    """ONE candidate against ONE snapshot — pure geometry, no driver."""
    if isinstance(c, RoleNameLocator):
        return _resolve_role_name(snap, c)
    if isinstance(c, LabelRelativeLocator):
        if c.relation == "right_of_label":
            return _resolve_right_of(snap, c)
        if c.relation == "below_label":
            return _resolve_below(snap, c)
        return _resolve_row_cell(snap, c)
    if isinstance(c, ExactTextLocator):
        return _resolve_exact_text(snap, c)
    return _resolve_bbox(snap, c, viewport)


# --- fingerprints ----------------------------------------------------------------------------


def fingerprint_ok(fp: ElementFingerprint, tier: LocatorTier, role: str, control_type: str, name: str, text: str) -> bool:
    """Per-tier strictness (SPEC §4). ``None`` is a wildcard."""
    if fp.role is not None and fp.role != role:
        return False
    if fp.control_type != control_type:
        return False
    if tier is LocatorTier.VISUAL:
        return True
    if fp.name_snippet is not None and norm(fp.name_snippet) not in norm(name):
        return False
    if fp.text_snippet is not None and norm(fp.text_snippet) not in norm(text):
        return False
    return True


# --- the walk ----------------------------------------------------------------------------------


@dataclass
class WalkResult:
    element: ResolvedElement
    resolution: LocatorResolution
    drift_flag: bool
    #: Which candidates were tried and why they failed (for FailureDetail.observed_summary).
    attempts: list[str]


def walk_ladder(
    surface: Surface,
    ladder: LocatorLadder,
    params: Mapping[str, str],
    source: Literal["artifact", "overlay"] = "artifact",
    attempts_out: list[str] | None = None,
) -> WalkResult | None:
    """Try each candidate in order; a fingerprint mismatch is treated as unresolved. When nothing
    resolves, ``attempts_out`` (if given) receives the per-candidate reasons."""
    expanded = expand_ladder(ladder, params)
    attempts: list[str] = attempts_out if attempts_out is not None else []
    for i, cand in enumerate(expanded.candidates):
        el = surface.resolve(cand)
        if el is None:
            attempts.append(f"[{i}] tier {int(cand.tier)} {cand.kind}: no match")
            continue
        if not fingerprint_ok(expanded.fingerprint, cand.tier, el.role, el.control_type, el.name, el.text):
            attempts.append(
                f"[{i}] tier {int(cand.tier)} {cand.kind}: fingerprint mismatch "
                f"(got role={el.role!r} control_type={el.control_type!r} name={el.name[:40]!r})"
            )
            continue
        return WalkResult(
            element=el,
            resolution=LocatorResolution(tier=cand.tier, candidate_index=i, source=source, fingerprint_ok=True),
            drift_flag=cand.tier is LocatorTier.VISUAL,
            attempts=attempts,
        )
    return None


def resolve_ladder_until(
    surface: Surface,
    ladder: LocatorLadder,
    params: Mapping[str, str],
    deadline_s: float,
    *,
    source: Literal["artifact", "overlay"] = "artifact",
    tick_s: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> WalkResult | None:
    """Re-observe and walk the ladder every ``tick_s`` until it resolves or ``deadline_s``
    (a ``time.monotonic()`` value) passes — the iframe pane may land a tick after the shell
    (SPEC §9: ladder resolution is polled within the step budget)."""
    while True:
        surface.observe()
        res = walk_ladder(surface, ladder, params, source=source)
        if res is not None:
            return res
        remaining = deadline_s - time.monotonic()
        if remaining <= 0:
            return None
        sleep(min(tick_s, remaining))
