"""Element descriptors (SPEC §10.2) computed from snapshot GEOMETRY — the same shape the human-action
recorder emits (§11), so the distiller has one input format.

``label_context`` = the nearest text-bearing static element to the LEFT in the same row (a form
field's label cell, a value cell's label). ``row_anchor``/``col_header`` are filled only when the
element sits in a table with a header row: rows are cells grouped by vertical band; walking up
row by row while the rows touch (border-collapse tables) AND share the column grid reaches that
table's first row, which is its header row; a table with fewer than three columns is a label/value
form and has no header row (its labels anchor ``right_of_label`` instead). The anchor is the leftmost
unmasked cell of the element's row.
"""

from __future__ import annotations

from anton.policy.redact import MASK
from anton.replay.locate import CELL_ROLES, INTERACTIVE_ROLES, _same_column, _same_row
from anton.schema.snapshot import ElementDescriptor, SemanticSnapshot, SnapshotElement

__all__ = ["describe"]

STATIC_TEXT_ROLES = CELL_ROLES | {"text", "heading", "label"}
ROW_GAP_PX = 3.0
#: Fewer columns than this is a label/value table (no header row), not a data table.
MIN_TABLE_COLUMNS = 3


def _left_label(snap: SemanticSnapshot, el: SnapshotElement) -> str | None:
    best: tuple[float, SnapshotElement] | None = None
    for other in snap.elements:
        if other is el or not other.visible or other.frame_path != el.frame_path:
            continue
        if other.role not in STATIC_TEXT_ROLES or not other.text.strip() or other.text == MASK:
            continue
        if other.bbox.x + other.bbox.w > el.bbox.x + 1 or not _same_row(other.bbox, el.bbox):
            continue
        d = el.bbox.x - (other.bbox.x + other.bbox.w)
        if best is None or d < best[0]:
            best = (d, other)
    return best[1].text if best else None


def _rows(cells: list[SnapshotElement]) -> list[list[SnapshotElement]]:
    rows: list[list[SnapshotElement]] = []
    for c in sorted(cells, key=lambda e: (e.bbox.y, e.bbox.x)):
        for row in rows:
            if _same_row(row[0].bbox, c.bbox):
                row.append(c)
                break
        else:
            rows.append([c])
    return rows


def _table_header(snap: SemanticSnapshot, el: SnapshotElement) -> tuple[list[SnapshotElement], list[SnapshotElement]] | None:
    """(header row, element's row) when the element sits in a table with a header row above it."""
    cells = [e for e in snap.elements if e.visible and e.frame_path == el.frame_path and e.role in CELL_ROLES]
    rows = _rows(cells)
    mine = next((r for r in rows if any(c is el for c in r)), None)
    if mine is None:
        # an input inside a cell: use the cell that contains it
        container = next((c for c in cells if c.bbox.x <= el.bbox.x and c.bbox.y <= el.bbox.y and c.bbox.x + c.bbox.w >= el.bbox.x + el.bbox.w and c.bbox.y + c.bbox.h >= el.bbox.y + el.bbox.h), None)
        if container is None:
            return None
        mine = next(r for r in rows if any(c is container for c in r))
    if len(mine) < MIN_TABLE_COLUMNS:
        return None  # a 2-column label/value table has no header row: its labels anchor right_of_label
    idx = rows.index(mine)
    top = idx
    while top > 0:
        above, cur = rows[top - 1], rows[top]
        above_bottom = max(c.bbox.y + c.bbox.h for c in above)
        cur_top = min(c.bbox.y for c in cur)
        if cur_top - above_bottom > ROW_GAP_PX:
            break
        if len(above) != len(cur) or not all(_same_column(a.bbox, c.bbox) for a, c in zip(sorted(above, key=lambda e: e.bbox.x), sorted(cur, key=lambda e: e.bbox.x), strict=True)):
            break  # a title row / another table: same table rows share the column grid
        top -= 1
    if top == idx:
        return None
    return rows[top], mine


def describe(snap: SemanticSnapshot, el: SnapshotElement) -> ElementDescriptor:
    desc = ElementDescriptor(role=el.role, name=el.name, text=el.text, control_type=el.control_type, frame_path=list(el.frame_path), bbox=el.bbox)
    left = _left_label(snap, el)
    if left is None and el.role in INTERACTIVE_ROLES and el.name:
        # A control named by an attribute (aria-label/placeholder) has no visible label unless some
        # static element in the same frame actually shows that text — never fabricate an anchor.
        shown = any(o.visible and o.frame_path == el.frame_path and o.role in STATIC_TEXT_ROLES and o.text.strip() == el.name.strip() and o is not el for o in snap.elements)
        left = el.name if shown else None
    desc.label_context = left
    table = _table_header(snap, el)
    if table is not None:
        header, row = table
        col = next((h for h in header if _same_column(h.bbox, el.bbox) and h.text.strip()), None)
        anchor = next((c for c in sorted(row, key=lambda c: c.bbox.x) if c is not el and c.text.strip() and c.text != MASK), None)
        if col is not None and anchor is not None:
            desc.col_header = col.text
            desc.row_anchor = anchor.text
            anchor_col = next((h for h in header if _same_column(h.bbox, anchor.bbox) and h.text.strip()), None)
            desc.row_anchor_col_header = anchor_col.text if anchor_col else None
    return desc
