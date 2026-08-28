"""SPEC §16 "Locator ladder" (driver half), "Redaction" (screenshot masks), "Surface":
against the REAL target app through the REAL PlaywrightSurface."""

from __future__ import annotations

import time

import pytest

from anton.policy.redact import MASK
from anton.replay.locate import walk_ladder
from anton.schema.app_profile import MaskSpec
from anton.schema.artifact import (
    ActionType,
    ElementFingerprint,
    LabelRelativeLocator,
    LocatorLadder,
    LocatorTier,
)
from anton.surface.base import ActError, ConcreteAction
from anton.surface.web import PlaywrightSurface, solid_png
from tests.acceptance.conftest import Target, seeded_members
from tests.integration.conftest import MASKS, PANE, click_and_wait, login_via_surface, must, rn

pytestmark = pytest.mark.integration

CANONICAL = "204817"


def member(target: Target, surface: PlaywrightSurface, member_id: str = CANONICAL) -> None:
    surface.act(ConcreteAction(ActionType.NAVIGATE, url=f"{target.origin}{target.prefix}/member/{member_id}"), 8000)
    surface.page.wait_for_load_state("networkidle")
    surface.observe()


def row_cell(anchor_col: str | None = "Type") -> LabelRelativeLocator:
    return LabelRelativeLocator(tier=LocatorTier.ANCHORED, relation="row_cell", anchor_text="Savings", anchor_col_header=anchor_col, col_header="Balance", rationale="row/column anchoring", frame_path=PANE)


def test_login_page_names_come_from_sibling_label_cells_and_password_keeps_its_name(alpha: Target, make_surface) -> None:
    s = make_surface()
    s.act(ConcreteAction(ActionType.NAVIGATE, url=f"{alpha.origin}/login"), 8000)
    s.page.wait_for_load_state()
    snap = s.observe()
    names = {(e.role, e.name) for e in snap.elements}
    assert ("textbox", "Username") in names and ("textbox", "Password") in names and ("button", "Sign in") in names
    # Every element carries a top-frame path and a non-empty box; nothing came from ids/test-ids.
    assert all(e.frame_path == [] and e.bbox.w > 0 for e in snap.elements)
    login_via_surface(s, alpha)
    assert s.path() == "/search"
    assert must(s, rn("link", "Sign out")).control_type == "a"


def test_pane_is_frame_aware_redacted_at_build_and_masks_resolve_on_the_redacted_snapshot(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    member(alpha, s)
    snap = s._snapshot
    assert snap is not None and snap.frame_count == 2
    pane = [e for e in snap.elements if e.frame_path == PANE]
    top = [e for e in snap.elements if e.frame_path == []]
    assert pane and top
    iframe_top = min(e.bbox.y for e in pane)
    heading_y = next(e.bbox.y for e in top if e.text == "Member Profile")
    assert iframe_top > heading_y  # top-page pixels: pane content sits below the shell heading
    m = next(m for m in seeded_members() if m["member_id"] == CANONICAL)
    texts = " ".join(e.text + " " + e.name for e in snap.elements)
    assert m["ssn"] not in texts and m["dob"] not in texts  # scrubbed at build (mask region + label context)
    assert any(e.text == "SSN" and e.frame_path == PANE for e in pane)  # the label cell survives
    assert any(e.text == MASK and e.frame_path == PANE for e in pane)
    # The SSN mask ladder resolves on the redacted snapshot (anchor kept), and read() is LIVE.
    ssn_cell = must(s, MASKS[0].ladder.candidates[0])
    assert s.read(ssn_cell, "text") == m["ssn"]
    # Tier-2 row_cell extraction target: resolves, reads the live balance.
    savings = next(a for a in m["accounts"] if a["type"] == "Savings")
    cell = must(s, row_cell())
    assert s.read(cell, "text") == f"${savings['balance']:,.2f}"


def test_row_cell_survives_beta_column_shift_and_relabelled_search_field(beta: Target, make_surface) -> None:
    s = make_surface(route_prefix="/portal")
    login_via_surface(s, beta)
    assert s.path() == "/search"  # prefix stripped
    assert must(s, rn("textbox", "Customer Number")).control_type == "input"
    assert s.resolve(rn("textbox", "Member ID")) is None  # tier-1 miss on beta …
    member(beta, s)
    m = next(m for m in seeded_members() if m["member_id"] == CANONICAL)
    savings = next(a for a in m["accounts"] if a["type"] == "Savings")
    cell = must(s, row_cell())  # … but row_cell finds the balance although Type is now the last column
    assert s.read(cell, "text") == f"${savings['balance']:,.2f}"


def test_walk_ladder_fingerprint_mismatch_falls_through_and_tier3_flags_drift(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    search = must(s, rn("button", "Search"))
    vw, vh = s._raw.viewport
    b = search.bbox
    from anton.schema.artifact import BBoxLocator

    ladder = LocatorLadder(
        candidates=[
            rn("button", "Search"),
            BBoxLocator(tier=LocatorTier.VISUAL, x=b.x / vw, y=b.y / vh, w=b.w / vw, h=b.h / vh, rationale="last resort"),
        ],
        fingerprint=ElementFingerprint(role="button", control_type="input"),  # wrong control_type: tier 1 must fall through
    )
    res = walk_ladder(s, ladder, {})
    assert res is None  # tier 3 also checks role + control_type → nothing resolves
    ladder_ok = LocatorLadder(candidates=list(ladder.candidates), fingerprint=ElementFingerprint(role="button", control_type="button", name_snippet="Sea"))
    res = walk_ladder(s, ladder_ok, {})
    assert res is not None and res.resolution.tier is LocatorTier.SEMANTIC and not res.drift_flag
    only_bbox = LocatorLadder(candidates=[ladder.candidates[1]], fingerprint=ElementFingerprint(role="button", control_type="button", name_snippet="zzz"))
    res = walk_ladder(s, only_bbox, {})
    assert res is not None and res.resolution.tier is LocatorTier.VISUAL and res.drift_flag  # tier 3 ignores the snippet


def test_param_token_expands_in_a_locator_name(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    last = next(m for m in seeded_members() if m["member_id"] == CANONICAL)["last_name"]
    s.act(ConcreteAction(ActionType.NAVIGATE, url=f"{alpha.origin}/search?last_name={last}"), 8000)
    s.page.wait_for_load_state()
    s.observe()
    ladder = LocatorLadder(candidates=[rn("link", "$param:member_id", exact=True)], fingerprint=ElementFingerprint(role="link", control_type="a"))
    res = walk_ladder(s, ladder, {"member_id": CANONICAL})
    assert res is not None and res.element.name == CANONICAL


def test_visibility_is_false_under_a_top_frame_overlay_even_inside_the_pane(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    member(alpha, s)
    assert s.resolve(row_cell()) is not None
    s.page.evaluate("() => { const d = document.createElement('div'); d.id = 'cover'; Object.assign(d.style, {position:'fixed', left:0, top:0, width:'100%', height:'100%', background:'rgba(0,0,0,.5)', zIndex: 99999}); document.body.appendChild(d); }")
    s.observe()
    assert s.resolve(row_cell()) is None and s.resolve(rn("link", "Sign out")) is None
    s.page.evaluate("() => document.getElementById('cover').remove()")
    s.observe()
    assert s.resolve(row_cell()) is not None


def test_modal_dismiss_through_the_surface(target_factory, make_surface) -> None:
    t = target_factory(tenant="alpha", inject=("modal",))
    s = make_surface()
    login_via_surface(s, t)
    assert s.resolve(rn("button", "Search")) is None  # behind the maintenance modal
    dismiss = must(s, rn("button", "Dismiss"))
    s.act(ConcreteAction(ActionType.CLICK, target=dismiss), 8000)
    s.observe()
    assert s.resolve(rn("button", "Dismiss")) is None and s.resolve(rn("button", "Search")) is not None


def test_action_timeout_equals_the_step_budget(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    search = must(s, rn("button", "Search"))
    s.page.evaluate("() => { const d = document.createElement('div'); Object.assign(d.style, {position:'fixed', left:0, top:0, width:'100%', height:'100%', zIndex: 99999}); document.body.appendChild(d); }")
    t0 = time.monotonic()
    with pytest.raises(ActError) as ei:
        s.act(ConcreteAction(ActionType.CLICK, target=search), 700)
    assert time.monotonic() - t0 < 5 and ei.value.kind in ("obscured", "timeout")


def test_native_dialog_is_logged_and_dismissed(alpha: Target, make_surface, events: list) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    result = s.page.evaluate("() => confirm('Delete everything? 900-11-2222')")
    assert result is False  # dismissed, never accepted
    assert any(k == "dialog" and d["type"] == "confirm" and "900-11-2222" not in d["message"] and MASK in d["message"] for k, d in events)


def test_screenshot_masks_inside_the_iframe_and_fails_closed(alpha: Target, make_surface, events: list) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    member(alpha, s)
    shot = s.screenshot()
    assert shot.exists() and shot.stat().st_size > 1000 and not [k for k, _ in events if k == "mask_unresolved"]
    # After capture the overlays are gone and the SSN cell still resolves.
    s.observe()
    assert s.resolve(MASKS[0].ladder.candidates[0]) is not None
    broken = MaskSpec(id="ghost", ladder=LocatorLadder(candidates=[rn("cell", "No Such Label", frame=PANE)], fingerprint=ElementFingerprint(control_type="td")), applies_to=r"^/member/[^/]+$")
    blanked = s.screenshot([broken])
    assert blanked.read_bytes() == solid_png(*s._raw.viewport)
    assert any(k == "mask_unresolved" and d["mask_id"] == "ghost" for k, d in events)


def test_confirmation_page_scrubs_the_account_number_but_reads_it_live(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    s.act(ConcreteAction(ActionType.NAVIGATE, url=f"{alpha.origin}/member/{CANONICAL}/subaccount/new"), 8000)
    s.page.wait_for_load_state()
    s.observe()
    s.act(ConcreteAction(ActionType.FILL, target=must(s, rn("textbox", "Nickname")), value="Holiday fund", value_kind="literal"), 8000)
    s.act(ConcreteAction(ActionType.FILL, target=must(s, rn("textbox", "Initial deposit")), value="50", value_kind="param"), 8000)
    s.act(ConcreteAction(ActionType.CLICK, target=must(s, rn("checkbox", "Terms"))), 8000)
    click_and_wait(s, must(s, rn("button", "Continue to review")))
    assert s.path().endswith("/subaccount/review")
    click_and_wait(s, must(s, rn("button", "Confirm and open account")))
    snap = s._snapshot
    assert snap is not None
    assert s.path().endswith("/subaccount/confirmation")
    label = next(e for e in snap.elements if e.text == "Account Number")
    value_el = must(s, MASKS[2].ladder.candidates[0])
    assert label.text == "Account Number"  # anchor kept
    assert next(e for e in snap.elements if e.ref == value_el.ref).text == MASK  # value scrubbed in the snapshot …
    assert s.read(value_el, "text").startswith("7204817")  # … but readable live for the caller


def _png_pixel(path, x: int, y: int) -> tuple[int, int, int]:
    """Decode one pixel of an 8-bit RGB/RGBA PNG with the stdlib (filters 0–4)."""
    import struct
    import zlib

    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h, ctype = 8, b"", 0, 0, 0
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if kind == b"IHDR":
            w, h, _bd, ctype = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + ln
    bpp = 4 if ctype == 6 else 3
    stride = w * bpp
    raw = zlib.decompress(idat)
    prev = bytearray(stride)
    rows = []
    for r in range(h):
        f = raw[r * (stride + 1)]
        line = bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        rows.append(bytes(line))
        prev = line
    px = rows[y][x * bpp:x * bpp + 3]
    return px[0], px[1], px[2]


def test_screenshot_mask_is_painted_black_over_the_ssn_cell(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    member(alpha, s)
    cell = must(s, MASKS[0].ladder.candidates[0])
    shot = s.screenshot()
    cx, cy = int(cell.bbox.x + cell.bbox.w / 2), int(cell.bbox.y + cell.bbox.h / 2)
    assert _png_pixel(shot, cx, cy) == (0, 0, 0)
    assert _png_pixel(shot, 5, int(cell.bbox.y + cell.bbox.h / 2)) != (0, 0, 0)  # left margin, outside the mask


def test_no_seeded_pii_leaks_without_any_mask_ladder(alpha: Target, make_surface) -> None:
    """Rule (3) by label context is the fail-safe when no mask is configured."""
    s = make_surface(masks=[])
    login_via_surface(s, alpha)
    member(alpha, s)
    snap = s._snapshot
    m = next(m for m in seeded_members() if m["member_id"] == CANONICAL)
    blob = " ".join(e.text + " " + e.name + " " + (e.value or "") for e in snap.elements)
    assert m["ssn"] not in blob and m["dob"] not in blob
    for a in m["accounts"]:
        assert a["account_no"] not in blob
    assert any(e.text == "SSN" for e in snap.elements) and any(e.text == "Account No." for e in snap.elements)


def test_visibility_is_per_element_under_a_partial_top_frame_overlay(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    member(alpha, s)
    ssn = must(s, MASKS[0].ladder.candidates[0])
    b = ssn.bbox
    # Cover only the SSN row (top-page coords) — the iframe centre stays uncovered.
    s.page.evaluate(
        "([x,y,w,h]) => { const d = document.createElement('div'); d.id = 'strip'; Object.assign(d.style, {position:'fixed', left:x+'px', top:y+'px', width:w+'px', height:h+'px', background:'#f00', zIndex: 99999}); document.body.appendChild(d); }",
        [b.x - 200, b.y - 2, b.w + 400, b.h + 4],
    )
    s.observe()
    assert s.resolve(MASKS[0].ladder.candidates[0]) is None  # the SSN value cell is covered …
    assert s.resolve(row_cell()) is not None  # … but the accounts table below is not
    s.page.evaluate("() => document.getElementById('strip').remove()")


def test_resolve_after_screenshot_on_a_changed_page_stays_consistent(alpha: Target, make_surface) -> None:
    s = make_surface()
    login_via_surface(s, alpha)
    member(alpha, s)
    s.act(ConcreteAction(ActionType.NAVIGATE, url=f"{alpha.origin}/search"), 8000)
    s.page.wait_for_load_state()
    s.screenshot()  # must not silently re-pair refs with a stale snapshot
    stale = s.resolve(row_cell())  # stale snapshot: either None or a consistent element, never a crash
    s.observe()
    fresh = s.resolve(rn("button", "Search"))
    assert fresh is not None and fresh.name == "Search"
    assert stale is None or stale.ref != fresh.ref


def test_ladder_resolution_polls_through_the_pane_load_race(target_factory, make_surface) -> None:
    import time

    from anton.replay.locate import resolve_ladder_until

    t = target_factory(tenant="alpha", inject=("slow",), slow_ms=1500)
    s = make_surface()
    login_via_surface(s, t)
    s.act(ConcreteAction(ActionType.NAVIGATE, url=f"{t.origin}/member/{CANONICAL}"), 8000)  # commit only; the slow shell + pane land later
    ladder = LocatorLadder(candidates=[row_cell()], fingerprint=ElementFingerprint(role="cell", control_type="td"))
    t0 = time.monotonic()
    res = resolve_ladder_until(s, ladder, {}, time.monotonic() + 8)
    assert res is not None and time.monotonic() - t0 < 8
    assert s.read(res.element, "text").startswith("$")
