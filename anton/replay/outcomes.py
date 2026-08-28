"""Detector evaluation + classification (SPEC §4 detector union, §9 tick order).

Pure over an ``Evaluator`` and a snapshot: which detector fires first (``any_of`` over ``when``,
declaration order, suppressed ids skipped) and which arm it carries. The engine decides what the
arm MEANS (stop, recover, escalate); nothing here touches a driver.
"""

from __future__ import annotations

from typing import Literal

from anton.replay.predicates import Evaluator
from anton.schema.artifact import BusinessDetector, HardFailureDetector, OutcomeDetector, RecoverableDetector
from anton.schema.snapshot import SemanticSnapshot

__all__ = ["Arm", "first_hit", "arm_of", "recoverable"]

Arm = Literal["RECOVERABLE", "BUSINESS_OUTCOME", "HARD_FAILURE"]


def first_hit(detectors: list[OutcomeDetector], ev: Evaluator, snap: SemanticSnapshot, suppress: set[str] = frozenset()) -> OutcomeDetector | None:  # type: ignore[assignment]
    """The first detector (declaration order) whose ``when`` list has any predicate holding."""
    for d in detectors:
        if d.id in suppress:
            continue
        if ev.any_holds(d.when, snap):
            return d
    return None


def arm_of(det: OutcomeDetector) -> Arm:
    if isinstance(det, RecoverableDetector):
        return "RECOVERABLE"
    if isinstance(det, BusinessDetector):
        return "BUSINESS_OUTCOME"
    assert isinstance(det, HardFailureDetector)
    return "HARD_FAILURE"


def recoverable(detectors: list[OutcomeDetector]) -> list[OutcomeDetector]:
    return [d for d in detectors if isinstance(d, RecoverableDetector)]
