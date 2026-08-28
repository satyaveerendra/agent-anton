"""SPEC §16 "Locator ladder" (geometry half, no driver): tiers, relations, fingerprints, tokens."""

from __future__ import annotations

import pytest

from anton.replay.locate import expand_ladder, expand_text, fingerprint_ok, resolve_candidate, walk_ladder
from anton.schema.artifact import (
    BBoxLocator,
    ElementFingerprint,
    ExactTextLocator,
    LabelRelativeLocator,
    LocatorCandidate,
    LocatorLadder,
    LocatorTier,
    RoleNameLocator,
)
from anton.schema.snapshot import BBox, SemanticSnapshot, SnapshotElement
from anton.surface.base import ResolvedElement

PANE = ["iframe[name=detail]"]
VIEWPORT = (1000, 800)


def el(ref: str, role: str, text: str, x: float, y: float, w: float = 100, h: float = 20, *, name: str | None = None,
       control_type: str = "td", frame: list[str] | None = None, visible: bool = True) -> SnapshotElement:
    return SnapshotElement(ref=ref, role=role, name=text if name is None else name, text=text, control_type=control_type,
                           frame_path=frame or [], bbox=BBox(x=x, y=y, w=w, h=h), enabled=True, visible=visible)


def snap(*elements: SnapshotElement) -> SemanticSnapshot:
    return SemanticSnapshot(url="http://x/", title="t", frame_count=2, elements=list(elements))


def accounts_table(order: list[str], y0: float = 300) -> list[SnapshotElement]:
    """A header row + two rows, columns laid out left→right in ``order`` (alpha vs beta)."""
    headers = {"type": "Type", "account_no": "Account No.", "balance": "Balance"}
    rows = [{"type": "Checking", "account_no": "120481701", "balance": "$1,000.00"}, {"type": "Savings", "account_no": "220481701", "balance": "$19,823.28"}]
    out: list[SnapshotElement] = []
    n = 0
    for ci, col in enumerate(order):
        n += 1
        out.append(el(f"h{n}", "cell", headers[col], 10 + ci * 120, y0, frame=PANE))
    for ri, row in enumerate(rows):
        for ci, col in enumerate(order):
            n += 1
            out.append(el(f"r{ri}c{ci}", "cell", row[col], 10 + ci * 120, y0 + 25 * (ri + 1), frame=PANE))
    return out


def row_cell(**kw) -> LabelRelativeLocator:
    base = dict(tier=LocatorTier.ANCHORED, relation="row_cell", anchor_text="Savings", anchor_col_header="Type", col_header="Balance", rationale="r", frame_path=PANE)
    base.update(kw)
    return LabelRelativeLocator(**base)


def test_tier1_role_name_exact_and_contains_and_frame_aware() -> None:
    s = snap(el("e1", "button", "Search", 0, 0, control_type="button"), el("e2", "button", "Search", 0, 0, control_type="button", frame=PANE), el("e3", "link", "Sign out", 0, 0, control_type="a"))
    c = RoleNameLocator(tier=1, role="button", name="search", rationale="r")
    assert resolve_candidate(s, c, VIEWPORT).ref == "e1"
    assert resolve_candidate(s, c.model_copy(update={"frame_path": PANE}), VIEWPORT).ref == "e2"
    assert resolve_candidate(s, RoleNameLocator(tier=1, role="link", name="Sign", rationale="r"), VIEWPORT).ref == "e3"
    assert resolve_candidate(s, RoleNameLocator(tier=1, role="link", name="Sign", exact=True, rationale="r"), VIEWPORT) is None
    hidden = snap(el("e1", "button", "Search", 0, 0, control_type="button", visible=False))
    assert resolve_candidate(hidden, c, VIEWPORT) is None


def test_row_cell_survives_a_column_shift_that_moves_the_anchor_column_too() -> None:
    alpha = snap(*accounts_table(["type", "account_no", "balance"]))
    beta = snap(*accounts_table(["account_no", "balance", "type"]))
    for s in (alpha, beta):
        got = resolve_candidate(s, row_cell(), VIEWPORT)
        assert got is not None and got.text == "$19,823.28", s
    # anchor_col_header restricts the anchor to the Type column (a nickname "Savings" elsewhere must not match).
    decoy = snap(el("d1", "cell", "Savings", 400, 300, frame=PANE), el("d2", "cell", "Balance", 400, 280, frame=PANE), *accounts_table(["account_no", "balance", "type"]))
    assert resolve_candidate(decoy, row_cell(), VIEWPORT).text == "$19,823.28"
    assert resolve_candidate(beta, row_cell(col_header="Account No."), VIEWPORT).text == "220481701"
    assert resolve_candidate(beta, row_cell(anchor_text="Money Market"), VIEWPORT) is None


def test_right_of_label_and_below_label() -> None:
    s = snap(
        el("l1", "cell", "SSN", 0, 100, frame=PANE), el("v1", "cell", "900-37-2769", 110, 100, frame=PANE),
        el("l2", "cell", "Member ID", 0, 10), el("i2", "textbox", "", 110, 10, name="Member ID", control_type="input"),
        el("l3", "text", "Nickname", 0, 200), el("i3", "textbox", "", 0, 230, name="Nickname", control_type="input"),
    )
    right = LabelRelativeLocator(tier=2, relation="right_of_label", anchor_text="SSN", rationale="r", frame_path=PANE)
    assert resolve_candidate(s, right, VIEWPORT).ref == "v1"
    right2 = LabelRelativeLocator(tier=2, relation="right_of_label", anchor_text="Member ID", target_role="textbox", rationale="r")
    assert resolve_candidate(s, right2, VIEWPORT).ref == "i2"
    below = LabelRelativeLocator(tier=2, relation="below_label", anchor_text="Nickname", rationale="r")
    assert resolve_candidate(s, below, VIEWPORT).ref == "i3"
    assert resolve_candidate(s, right.model_copy(update={"frame_path": []}), VIEWPORT) is None  # frame-aware


def test_exact_text_and_bbox_pick_smallest_containing_box() -> None:
    s = snap(el("big", "text", "Please correct the errors below.", 0, 0, w=800, h=200), el("small", "text", "Initial deposit must be at least $25.", 300, 50, w=200, h=20))
    assert resolve_candidate(s, ExactTextLocator(tier=2, text="please correct the errors below.", rationale="r"), VIEWPORT).ref == "big"
    box = BBoxLocator(tier=3, x=0.3, y=0.05, w=0.2, h=0.025, rationale="r")  # centre (400, 50) — inside both
    assert resolve_candidate(s, box, VIEWPORT).ref == "small"


def test_fingerprint_strictness_per_tier() -> None:
    fp = ElementFingerprint(role="cell", control_type="td", name_snippet="Sav", text_snippet="Sav")
    assert fingerprint_ok(fp, LocatorTier.SEMANTIC, "cell", "td", "Savings", "Savings")
    assert not fingerprint_ok(fp, LocatorTier.ANCHORED, "cell", "td", "Checking", "Checking")  # name_snippet checked at tiers 1–2
    assert fingerprint_ok(fp, LocatorTier.VISUAL, "cell", "td", "Checking", "Checking")  # tier 3: role + control_type only
    assert not fingerprint_ok(fp, LocatorTier.VISUAL, "cell", "th", "Savings", "Savings")
    assert fingerprint_ok(ElementFingerprint(control_type="td"), LocatorTier.SEMANTIC, "cell", "td", "anything", "")  # None is a wildcard


def test_param_token_expansion_uses_the_original_string() -> None:
    assert expand_text("Member $param:member_id", {"member_id": "204817"}) == "Member 204817"
    with pytest.raises(KeyError, match="ghost"):
        expand_text("$param:ghost", {"member_id": "1"})
    ladder = LocatorLadder(candidates=[RoleNameLocator(tier=1, role="link", name="$param:member_id", rationale="r")], fingerprint=ElementFingerprint(control_type="a", name_snippet="$param:member_id"))
    out = expand_ladder(ladder, {"member_id": "310552"})
    assert out.candidates[0].name == "310552" and out.fingerprint.name_snippet == "310552"  # type: ignore[union-attr]


class FakeSurface:
    """Resolves over a fixed snapshot; the ladder walk never touches a driver."""

    def __init__(self, s: SemanticSnapshot) -> None:
        self.s = s

    def resolve(self, candidate: LocatorCandidate) -> ResolvedElement | None:
        e = resolve_candidate(self.s, candidate, VIEWPORT)
        if e is None:
            return None
        return ResolvedElement(ref=e.ref, frame_path=e.frame_path, bbox=e.bbox, control_type=e.control_type, role=e.role, name=e.name, handle=None)


def test_walk_ladder_falls_through_on_fingerprint_mismatch_and_flags_tier3_drift() -> None:
    s = snap(el("e1", "button", "Search", 100, 100, w=80, h=30, control_type="button"), el("e2", "button", "Search", 100, 200, w=80, h=30, control_type="input"))
    ladder = LocatorLadder(
        candidates=[
            RoleNameLocator(tier=1, role="button", name="Search", rationale="r"),  # resolves e1 (button) — wrong control_type
            BBoxLocator(tier=3, x=0.1, y=0.25, w=0.08, h=0.0375, rationale="r"),  # centre (140, 215) → e2
        ],
        fingerprint=ElementFingerprint(role="button", control_type="input"),
    )
    res = walk_ladder(FakeSurface(s), ladder, {})
    assert res is not None and res.element.ref == "e2" and res.resolution.tier is LocatorTier.VISUAL
    assert res.resolution.candidate_index == 1 and res.drift_flag is True and "fingerprint mismatch" in res.attempts[0]
    good = LocatorLadder(candidates=[RoleNameLocator(tier=1, role="button", name="Search", rationale="r")], fingerprint=ElementFingerprint(role="button", control_type="button"))
    res2 = walk_ladder(FakeSurface(s), good, {}, source="overlay")
    assert res2 is not None and res2.resolution.tier is LocatorTier.SEMANTIC and res2.resolution.source == "overlay" and not res2.drift_flag
    assert walk_ladder(FakeSurface(snap()), good, {}) is None


def test_label_anchors_are_never_controls() -> None:
    s = snap(el("l", "cell", "Password", 0, 0), el("i", "textbox", "", 110, 0, name="Password", control_type="input"), el("v", "cell", "x", 220, 0))
    right = LabelRelativeLocator(tier=2, relation="right_of_label", anchor_text="Password", rationale="r")
    assert resolve_candidate(s, right, VIEWPORT).ref == "i"  # the input never anchors itself


def test_walk_ladder_reports_attempts_when_nothing_resolves() -> None:
    from anton.replay.locate import resolve_ladder_until

    s = snap(el("e1", "button", "Search", 100, 100, w=80, h=30, control_type="button"))
    ladder = LocatorLadder(candidates=[RoleNameLocator(tier=1, role="button", name="Nope", rationale="r")], fingerprint=ElementFingerprint(control_type="button"))
    attempts: list[str] = []
    assert walk_ladder(FakeSurface(s), ladder, {}, attempts_out=attempts) is None and attempts == ["[0] tier 1 role_name: no match"]

    class Late(FakeSurface):
        def __init__(self, s: SemanticSnapshot, after: int) -> None:
            super().__init__(snap())
            self.final, self.after, self.n = s, after, 0

        def observe(self) -> SemanticSnapshot:
            self.n += 1
            if self.n >= self.after:
                self.s = self.final
            return self.s

    import time

    good = LocatorLadder(candidates=[RoleNameLocator(tier=1, role="button", name="Search", rationale="r")], fingerprint=ElementFingerprint(control_type="button"))
    late = Late(s, after=3)
    res = resolve_ladder_until(late, good, {}, time.monotonic() + 5, tick_s=0.001, sleep=lambda _t: None)
    assert res is not None and late.n == 3  # resolved on the third tick, well within the deadline
    assert resolve_ladder_until(Late(s, after=99), good, {}, time.monotonic() + 0.01, tick_s=0.001) is None
