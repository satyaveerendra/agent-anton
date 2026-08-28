"""SemanticSnapshot builder (SPEC §7): injected-JS enumeration per frame, redacted at build.

Enumerates, across all frames, interactive elements, landmarks, headings, table cells and
every text-bearing block-level container (role ``text``). Roles are computed from tag/type/
ARIA; names from aria-label / <label for> / the nearest label text (the preceding cell in the
same row on this app's table-driven forms) — never from ids or test attributes. Every element
also carries a ``label_context`` (preceding label cell, or the column header above it) so the
Redactor's rule (3) can scrub value cells even when no mask ladder covers them.

Visibility is recursive and per element: an element is visible iff it intersects the top
viewport, its own document paints it at its centre, AND — for framed content — every ancestor
document's ``elementFromPoint`` at that centre (mapped into the ancestor's coordinates) is the
containing iframe. Boxes are TOP-PAGE CSS pixels (frame offsets applied). Refs are turn-scoped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from anton.policy.redact import RawElement, Redactor
from anton.schema.snapshot import BBox, SemanticSnapshot

__all__ = ["ENUMERATE_JS", "HIT_TEST_JS", "FrameInfo", "RawSnapshot", "build_snapshot"]

#: Evaluated per frame. Stashes the live elements in ``window.__anton_els`` so a ref can be
#: turned back into a handle (``frame.evaluate_handle("i => window.__anton_els[i]", idx)``).
ENUMERATE_JS = r"""
() => {
  const INTERACTIVE = 'a[href],button,input,select,textarea,[role="button"],[role="link"],[role="textbox"],[role="checkbox"]';
  const SKIP = new Set(['script','style','noscript','option','br','tbody','thead','tfoot','tr','table','html','head','meta','link','title','iframe','svg','path']);
  const LANDMARKS = new Set(['nav','main','header','footer','form','aside']);
  const vw = window.innerWidth, vh = window.innerHeight;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const directText = el => { let t = ''; for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent; return norm(t); };
  const roleOf = el => {
    const r = el.getAttribute('role'); if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'text';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'hidden') return null;
      return 'textbox';
    }
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'th') return 'columnheader';
    if (tag === 'td') return 'cell';
    if (tag === 'li') return 'listitem';
    if (tag === 'img') return 'img';
    if (tag === 'label') return 'label';
    if (LANDMARKS.has(tag)) return 'landmark';
    return 'text';
  };
  const prevCellText = cell => { let p = cell.previousElementSibling; while (p) { const t = norm(p.textContent); if (t) return t; p = p.previousElementSibling; } return null; };
  const headerAbove = cell => {
    const row = cell.parentElement; const table = cell.closest('table');
    if (!row || !table) return null;
    const first = table.querySelector('tr'); if (!first || first === row) return null;
    const idx = Array.prototype.indexOf.call(row.children, cell);
    const h = first.children[idx]; return h ? norm(h.textContent) || null : null;
  };
  const labelContext = el => {
    const id = el.getAttribute('id');
    if (id) { const l = document.querySelector('label[for="' + CSS.escape(id) + '"]'); if (l) return norm(l.textContent); }
    const lab = el.closest('label'); if (lab) { const t = norm(lab.textContent); if (t) return t; }
    const cell = el.closest('td,th');
    if (cell) { const t = prevCellText(cell); if (t) return t; const h = headerAbove(cell); if (h) return h; }
    let p = el.previousElementSibling; while (p) { const t = norm(p.textContent); if (t) return t; p = p.previousElementSibling; }
    const parent = el.parentElement; if (parent) { const t = directText(parent); if (t) return t; }
    return null;
  };
  const isFirstRow = cell => { const row = cell.parentElement; const table = cell.closest('table'); return !!(row && table && table.querySelector('tr') === row); };
  const staticContext = el => {
    // Both the preceding label cell (2-column label/value tables) and the column header above
    // (tables with a header row) are label context; either may name a sensitive field.
    // Cells of a table's FIRST row are headers/labels themselves and never take a neighbour's context.
    const cell = el.closest('td,th');
    if (cell) {
      if (isFirstRow(cell)) return null;
      const parts = [prevCellText(cell), headerAbove(cell)].filter(Boolean); return parts.length ? parts.join(' | ') : null;
    }
    let p = el.previousElementSibling; while (p) { const t = norm(p.textContent); if (t) return t; p = p.previousElementSibling; }
    return null;
  };
  const els = []; const out = [];
  const nodes = document.body ? document.body.querySelectorAll('*') : [];
  for (const el of nodes) {
    const tag = el.tagName.toLowerCase();
    if (SKIP.has(tag)) continue;
    if (el.hasAttribute('data-anton-mask')) continue;
    const role = roleOf(el); if (!role) continue;
    const inter = el.matches(INTERACTIVE);
    const dt = directText(el);
    const landmark = LANDMARKS.has(tag) || role === 'landmark';
    if (!inter && !landmark && role !== 'heading' && role !== 'img' && !dt) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    let visible = r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh;
    if (visible) { const st = getComputedStyle(el); if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') visible = false; }
    if (visible) {
      const hit = document.elementFromPoint(Math.min(Math.max(cx, 0), vw - 1), Math.min(Math.max(cy, 0), vh - 1));
      visible = !!hit && (hit === el || el.contains(hit));
    }
    const ctx = inter ? labelContext(el) : (landmark ? null : staticContext(el));
    let text;
    if (tag === 'select') { const o = el.selectedOptions && el.selectedOptions[0]; text = norm(o ? o.textContent : ''); }
    else if (tag === 'input' || tag === 'textarea') text = norm(el.value || '');
    else if (landmark && !dt) text = '';
    else text = norm(el.textContent).slice(0, 200);
    let name;
    const al = el.getAttribute('aria-label');
    if (al) name = norm(al);
    else if (inter && (tag === 'input' || tag === 'select' || tag === 'textarea') && role !== 'button') name = ctx || norm(el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '');
    else if (tag === 'input') name = norm(el.value || el.getAttribute('title') || '');
    else name = text || norm(el.getAttribute('alt') || el.getAttribute('title') || '');
    const value = (tag === 'input' || tag === 'textarea' || tag === 'select') ? (el.value ?? null) : null;
    const checked = (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio')) ? !!el.checked : null;
    els.push(el);
    out.push({ idx: els.length - 1, role, name, text, control_type: tag, bbox: [r.left, r.top, r.width, r.height],
               enabled: !el.disabled, visible, value, checked, label_context: ctx, interactive: inter });
  }
  window.__anton_els = els;
  const iframes = [];
  document.querySelectorAll('iframe').forEach((f, i) => {
    const r = f.getBoundingClientRect();
    iframes.push({ index: i, name: f.getAttribute('name') || null, rect: [r.left + f.clientLeft, r.top + f.clientTop, r.width, r.height] });
  });
  return { url: location.href, title: document.title, elements: out, iframes, viewport: [vw, vh] };
}
"""

#: Evaluated in a PARENT frame: for each point (parent coordinates), is the element painted
#: there the child's iframe? Batched: one round-trip per (parent, child) pair.
HIT_TEST_JS = r"""
([idx, pts]) => {
  const f = document.querySelectorAll('iframe')[idx];
  return pts.map(([x, y]) => { const h = document.elementFromPoint(x, y); return !!f && h === f; });
}
"""


@dataclass
class FrameInfo:
    frame: Any                      # playwright Frame
    path: list[str]                 # iframe[name=…] segments from the top document
    offset: tuple[float, float]     # top-page pixel offset of this frame's content box
    #: (parent FrameInfo index, DOM index of this frame's <iframe> in the parent) — None for the top frame.
    parent: tuple[int, int] | None = None


@dataclass
class RawSnapshot:
    """The unredacted enumeration plus what the surface needs to turn refs into handles."""

    url: str
    title: str
    viewport: tuple[int, int]
    frames: list[FrameInfo]
    raw: list[RawElement]
    locator: dict[str, tuple[int, int]] = field(default_factory=dict)  # ref -> (frame index, local idx)


def _segment(name: str | None, index: int) -> str:
    return f"iframe[name={name}]" if name else f"iframe[{index}]"


def _walk_frames(page: Any) -> tuple[list[tuple[FrameInfo, dict]], tuple[int, int]]:
    """Evaluate the enumeration in every frame, top-down, computing paths and offsets.
    A frame mid-navigation contributes nothing."""
    results: list[tuple[FrameInfo, dict]] = []
    viewport = (0, 0)

    def visit(frame: Any, path: list[str], offset: tuple[float, float], parent: tuple[int, int] | None) -> None:
        nonlocal viewport
        try:
            data = frame.evaluate(ENUMERATE_JS)
        except Exception:  # execution context destroyed / detached: pending navigation
            return
        my_index = len(results)
        results.append((FrameInfo(frame=frame, path=path, offset=offset, parent=parent), data))
        if not path:
            viewport = (int(data["viewport"][0]), int(data["viewport"][1]))
        by_name = {f["name"]: f for f in data["iframes"] if f["name"]}
        by_index = {f["index"]: f for f in data["iframes"]}
        for i, child in enumerate(frame.child_frames):
            entry = by_name.get(child.name) if child.name else None
            if entry is None:
                entry = by_index.get(i)
            if entry is None:
                continue
            rect = entry["rect"]
            visit(child, path + [_segment(entry["name"], entry["index"])], (offset[0] + rect[0], offset[1] + rect[1]), (my_index, entry["index"]))

    visit(page.main_frame, [], (0.0, 0.0), None)
    return results, viewport


def _framed_visibility(results: list[tuple[FrameInfo, dict]], raw: list[RawElement], viewport: tuple[int, int]) -> None:
    """Per-element recursive visibility: walk each framed element's ancestor chain and hit-test
    its top-page centre in every ancestor document; also require the top viewport to contain it."""
    vw, vh = viewport
    by_frame: dict[int, list[RawElement]] = {}
    frame_index_of: dict[str, int] = {}
    for i, (info, _data) in enumerate(results):
        frame_index_of["/".join(info.path)] = i
    for r in raw:
        if r.frame_path and r.visible:
            by_frame.setdefault(frame_index_of["/".join(r.frame_path)], []).append(r)
    for fi, elements in by_frame.items():
        for r in elements:
            b = r.bbox
            if b.x + b.w <= 0 or b.y + b.h <= 0 or b.x >= vw or b.y >= vh:
                r.visible = False
        elements = [r for r in elements if r.visible]
        level = fi
        while elements and results[level][0].parent is not None:
            parent_index, iframe_dom_index = results[level][0].parent  # type: ignore[misc]
            parent_info = results[parent_index][0]
            px, py = parent_info.offset
            points = [[r.bbox.x + r.bbox.w / 2 - px, r.bbox.y + r.bbox.h / 2 - py] for r in elements]
            try:
                hits = parent_info.frame.evaluate(HIT_TEST_JS, [iframe_dom_index, points])
            except Exception:
                hits = [False] * len(elements)
            for r, ok in zip(elements, hits, strict=True):
                if not ok:
                    r.visible = False
            elements = [r for r in elements if r.visible]
            level = parent_index


def build_snapshot(page: Any, redactor: Redactor, mask_regions_for: Any = None) -> tuple[SemanticSnapshot, RawSnapshot]:
    """Enumerate every frame, then redact (SPEC §8 rule 3). ``mask_regions_for(raw_snapshot)``
    returns the top-page boxes of the resolved ``sensitive_elements`` masks for this page."""
    results, viewport = _walk_frames(page)
    if not results:
        empty = SemanticSnapshot(url=page.url, title="", frame_count=0, elements=[])
        return empty, RawSnapshot(url=page.url, title="", viewport=viewport, frames=[], raw=[])
    raw: list[RawElement] = []
    frames: list[FrameInfo] = []
    locator: dict[str, tuple[int, int]] = {}
    n = 0
    for fi, (info, data) in enumerate(results):
        frames.append(info)
        ox, oy = info.offset
        for e in data["elements"]:
            n += 1
            ref = f"e{n}"
            x, y, w, h = e["bbox"]
            raw.append(
                RawElement(
                    ref=ref, role=e["role"], name=e["name"] or "", text=e["text"] or "", control_type=e["control_type"],
                    frame_path=list(info.path), bbox=BBox(x=x + ox, y=y + oy, w=w, h=h),
                    enabled=bool(e["enabled"]), visible=bool(e["visible"]),
                    value=e["value"], checked=e["checked"], label_context=e["label_context"], interactive=bool(e["interactive"]),
                )
            )
            locator[ref] = (fi, int(e["idx"]))
    _framed_visibility(results, raw, viewport)
    top = results[0][1]
    raw_snapshot = RawSnapshot(url=top["url"], title=top["title"], viewport=viewport, frames=frames, raw=raw, locator=locator)
    regions: list[BBox] = list(mask_regions_for(raw_snapshot)) if mask_regions_for is not None else []
    elements = redactor.scrub_elements(raw, regions)
    snapshot = SemanticSnapshot(url=top["url"], title=top["title"], frame_count=len(frames), elements=elements)
    return snapshot, raw_snapshot
