"""Predicate evaluation over a snapshot (SPEC §4 predicates, §9 tick loop).

``UrlMatches`` sees the prefix-stripped path, ``TitleMatches`` the document title, ``TextVisible``
the visible elements' (redacted) text, ``ElementVisible`` the ladder walk, ``FieldValue`` and
``Checked`` the LIVE control state via ``Surface.read`` (SPEC §9: "FieldValue predicates likewise
compare against the live value"). ``$param:`` tokens are expanded everywhere first.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from anton.replay.locate import expand_text, norm, walk_ladder
from anton.schema.artifact import (
    AllOf,
    Checked,
    ElementVisible,
    FieldValue,
    LiteralValue,
    ParamRef,
    Predicate,
    SecretRef,
    StateSignature,
    TextVisible,
    TitleMatches,
    UrlMatches,
)
from anton.schema.snapshot import SemanticSnapshot
from anton.surface.base import Surface

__all__ = ["Evaluator"]


class Evaluator:
    """Stateless apart from its inputs; the caller observes and passes the snapshot in."""

    def __init__(self, surface: Surface, params: Mapping[str, str]) -> None:
        self.surface = surface
        self.params = params

    def _rx(self, pattern: str) -> re.Pattern[str]:
        return re.compile(expand_text(pattern, self.params))

    def holds(self, pred: Predicate, snapshot: SemanticSnapshot) -> bool:
        if isinstance(pred, UrlMatches):
            return self._rx(pred.regex).search(self.surface.path()) is not None
        if isinstance(pred, TitleMatches):
            return self._rx(pred.regex).search(snapshot.title) is not None
        if isinstance(pred, TextVisible):
            rx = self._rx(pred.regex)
            return any(el.visible and rx.search(el.text) for el in snapshot.elements)
        if isinstance(pred, ElementVisible):
            return walk_ladder(self.surface, pred.ladder, self.params) is not None
        if isinstance(pred, FieldValue):
            res = walk_ladder(self.surface, pred.ladder, self.params)
            if res is None:
                return False
            live = self.surface.read(res.element, "value")
            exp = pred.expected
            if isinstance(exp, SecretRef):
                return live != ""
            wanted = exp.value if isinstance(exp, LiteralValue) else self.params.get(exp.name, "")
            if isinstance(exp, ParamRef) and exp.name not in self.params:
                return False
            return norm(live) == norm(expand_text(wanted, self.params))
        if isinstance(pred, Checked):
            res = walk_ladder(self.surface, pred.ladder, self.params)
            if res is None:
                return False
            return (self.surface.read(res.element, "checked") == "true") == pred.expected
        if isinstance(pred, AllOf):
            return all(self.holds(p, snapshot) for p in pred.predicates)
        raise TypeError(f"unknown predicate {type(pred).__name__}")  # pragma: no cover - exhaustive union

    def signature_holds(self, sig: StateSignature, snapshot: SemanticSnapshot) -> bool:
        return all(self.holds(p, snapshot) for p in sig.all_of)

    def any_holds(self, preds: list[Predicate], snapshot: SemanticSnapshot) -> bool:
        """Detector ``when`` lists have any_of semantics (SPEC §4)."""
        return any(self.holds(p, snapshot) for p in preds)
