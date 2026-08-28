"""Redactor — the only path to disk/stdout for run artifacts (SPEC §8).

``scrub(obj)`` replaces, in every string of every object it is given: (1) any value in the
run's known-sensitive set, (2) any match of ``redact_value_patterns``. Snapshot elements get
rule (3) at build time (``scrub_elements``): a value-bearing element inside a sensitive mask
region, or whose nearest-label context matches ``redact_field_patterns``, is scrubbed — but an
element whose OWN text is the matching label/header is the anchor and is never scrubbed, and an
interactive form control keeps its accessible ``name`` (only its ``value``/``text`` is scrubbed),
so the "Password" input's name still resolves the login ladder while the typed value never
appears anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from anton.schema.snapshot import BBox, SnapshotElement

MASK = "***"
#: Roles that are anchors when their own text matches a field pattern.
LABEL_ROLES = frozenset({"cell", "columnheader", "text", "heading", "listitem", "landmark", "label"})


@dataclass
class RawElement:
    """What the injected JS enumerates before redaction (SPEC §7 fields + the label context
    redaction needs). Only ``SnapshotElement`` fields ever leave this module."""

    ref: str
    role: str
    name: str
    text: str
    control_type: str
    frame_path: list[str]
    bbox: BBox
    enabled: bool
    visible: bool
    value: str | None
    checked: bool | None
    label_context: str | None
    interactive: bool

    def to_element(self) -> SnapshotElement:
        return SnapshotElement(
            ref=self.ref, role=self.role, name=self.name, text=self.text, control_type=self.control_type,
            frame_path=list(self.frame_path), bbox=self.bbox, enabled=self.enabled, visible=self.visible,
            value=self.value, checked=self.checked,
        )


def _center_in(box: BBox, region: BBox) -> bool:
    cx, cy = box.x + box.w / 2, box.y + box.h / 2
    return region.x <= cx <= region.x + region.w and region.y <= cy <= region.y + region.h


@dataclass
class Redactor:
    field_patterns: list[str]
    value_patterns: list[str]
    known_values: set[str] = field(default_factory=set)
    _field_rx: list[re.Pattern[str]] = field(init=False, repr=False)
    _value_rx: list[re.Pattern[str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._field_rx = [re.compile(p) for p in self.field_patterns]
        self._value_rx = [re.compile(p) for p in self.value_patterns]
        self.known_values = {v for v in self.known_values if self._worth_hiding(v)}

    @staticmethod
    def _worth_hiding(value: str) -> bool:
        # A one- or two-character "secret" would wipe every string it touches.
        return isinstance(value, str) and len(value.strip()) >= 3

    # --- rules (1) and (2): strings anywhere ---------------------------------------------

    def add_sensitive_value(self, value: object) -> None:
        if isinstance(value, str) and self._worth_hiding(value):
            self.known_values.add(value)

    def add_sensitive_values(self, values: Iterable[object]) -> None:
        for v in values:
            self.add_sensitive_value(v)

    def scrub_text(self, s: str) -> str:
        out = s
        for v in sorted(self.known_values, key=len, reverse=True):
            if v in out:
                out = out.replace(v, MASK)
        for rx in self._value_rx:
            out = rx.sub(MASK, out)
        return out

    def scrub(self, obj: Any) -> Any:
        """Recursively scrub every string in dicts, lists, tuples, pydantic models and scalars."""
        if isinstance(obj, str):
            return self.scrub_text(obj)
        if isinstance(obj, BaseModel):
            return self.scrub(obj.model_dump(mode="json"))
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.scrub(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.scrub(v) for v in obj)
        return obj

    # --- rule (3): snapshot elements at build time --------------------------------------------

    def is_field_label(self, text: str | None) -> bool:
        return bool(text) and any(rx.search(text) for rx in self._field_rx)

    def is_anchor_text(self, text: str | None) -> bool:
        """A short label/header whose own text names a sensitive field ("SSN", "Account No.")."""
        return bool(text) and len(text) <= 40 and self.is_field_label(text)

    def scrub_elements(self, raw: list[RawElement], mask_regions: list[BBox]) -> list[SnapshotElement]:
        out: list[SnapshotElement] = []
        for r in raw:
            el = r.to_element()
            in_mask = any(_center_in(r.bbox, region) for region in mask_regions)
            # Masks are authored to target VALUES, so a mask region always wins over the anchor exemption.
            is_anchor = (not in_mask) and (not r.interactive) and r.role in LABEL_ROLES and self.is_anchor_text(r.text)
            hit = (not is_anchor) and (in_mask or self.is_field_label(r.label_context))
            if hit:
                if r.interactive:
                    el.value = MASK if r.value else r.value
                    el.text = MASK if r.text else r.text
                else:
                    el.name = MASK if r.name else r.name
                    el.text = MASK if r.text else r.text
                    el.value = MASK if r.value else r.value
            # rules (1)/(2) apply to every string, anchors included (they never contain a value).
            el.name, el.text = self.scrub_text(el.name), self.scrub_text(el.text)
            if el.value is not None:
                el.value = self.scrub_text(el.value)
            out.append(el)
        return out
