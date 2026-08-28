"""Serialized perception structures (SPEC §7, §10.2).

``SemanticSnapshot`` is what the surface enumerates (redacted at build) and what the failure
"page snapshot" is written as; ``ElementDescriptor`` is the shape both the discovery loop and
the human-action recorder persist per action; ``TurnRecord`` is one line of ``turns.jsonl``.
``ResolvedElement`` (a live handle) is deliberately NOT here — it is never serialized and
lives in ``anton.surface.base``.
"""

from __future__ import annotations

from pydantic import Field

from anton.schema.artifact import ActionType, StrictModel, ValueRef

__all__ = ["BBox", "SnapshotElement", "SemanticSnapshot", "ElementDescriptor", "TurnRecord"]


class BBox(StrictModel):
    """TOP-PAGE CSS pixels (frame offsets applied)."""

    x: float
    y: float
    w: float = Field(ge=0)
    h: float = Field(ge=0)


class SnapshotElement(StrictModel):
    #: Turn-scoped (e1, e2…); never persisted into an artifact.
    ref: str
    role: str
    name: str
    text: str
    control_type: str
    #: Segments are ``iframe[name=<name>]``, or ``iframe[<index>]`` when unnamed.
    frame_path: list[str] = Field(default_factory=list)
    bbox: BBox
    enabled: bool
    visible: bool
    value: str | None = None
    checked: bool | None = None


class SemanticSnapshot(StrictModel):
    """Redacted at build; this is the failure "page snapshot" format."""

    url: str
    title: str
    frame_count: int = Field(ge=0)  # 0 only for the empty pending-navigation snapshot (§7)
    elements: list[SnapshotElement] = Field(default_factory=list)

    def by_ref(self, ref: str) -> SnapshotElement | None:
        for el in self.elements:
            if el.ref == ref:
                return el
        return None


class ElementDescriptor(StrictModel):
    """The same shape the human-action recorder emits (SPEC §11)."""

    role: str
    name: str
    text: str
    control_type: str
    frame_path: list[str] = Field(default_factory=list)
    bbox: BBox
    #: Nearest row/label text.
    label_context: str | None = None
    #: When the element sat in a table with a header row.
    row_anchor: str | None = None
    col_header: str | None = None
    #: Header of the anchor's column (decisions P5-3): what ``row_cell.anchor_col_header`` needs.
    row_anchor_col_header: str | None = None


class TurnRecord(StrictModel):
    turn: int = Field(ge=0)
    digest_path: str
    screenshot_path: str
    rationale: str
    tool: str
    #: ``finish`` carries {"outputs": {name: ref}}; ``act`` carries ref=None for NAVIGATE / global PRESS.
    args: dict[str, str | None | dict[str, str]] = Field(default_factory=dict)
    gate_decision: str
    executed: ElementDescriptor | None = None
    action: ActionType | None = None
    value_ref: ValueRef | None = None
    page_signature_after: str
