"""`make verify-target` (SPEC §19, phase-2 gate): curl every pinned page and every injection of
the REAL target app, asserting the pinned strings of SPEC §6."""

from __future__ import annotations

import json
import re
import time

import pytest

from tests.acceptance.conftest import CREDS, Target, seeded_members

pytestmark = pytest.mark.acceptance

CANONICAL = "204817"
SECOND = "310552"


def test_unauthenticated_302_to_login_with_next(alpha: Target) -> None:
    c = alpha.client()
    r = c.get("/search")
    assert r.status == 302 and r.location.endswith("/login?next=/search")
    r = c.get(f"/member/{CANONICAL}/subaccount/new")
    assert r.status == 302 and r.location.endswith(f"/login?next=/member/{CANONICAL}/subaccount/new")
    assert "reason=" not in r.location


def test_login_page_expired_message_and_bad_credentials(alpha: Target) -> None:
    c = alpha.client()
    r = c.get("/login?reason=expired")
    assert r.status == 200 and "Your session has expired. Please sign in again." in r.body
    assert "Sign out" not in r.body  # not authenticated yet
    r = c.get("/login?next=/member/204817")
    assert 'name="username"' in r.body and 'name="password"' in r.body and 'name="next" value="/member/204817"' in r.body
    r = c.post("/login", {"username": CREDS["username"], "password": "wrong"})
    assert r.status == 200 and "Invalid username or password." in r.body


def test_next_round_trips_through_the_login_post(alpha: Target) -> None:
    c = alpha.client()
    r = c.get(f"/member/{CANONICAL}")
    assert r.status == 302
    nxt = r.location.split("next=", 1)[1]
    r = c.login(next_path=nxt)
    assert r.status == 302 and r.location.endswith(f"/member/{CANONICAL}")
    r = c.get(f"/member/{CANONICAL}")
    assert r.status == 200 and "Member Profile" in r.body and "Sign out" in r.body
    assert '<iframe name="detail"' in r.body and f"/member/{CANONICAL}/pane" in r.body
    # Default next is /search.
    c2 = alpha.client()
    assert c2.login().location.endswith("/search")
    assert "Member Search" in c2.get("/search").body


def test_search_page_pinned_labels_and_submit_to_member(alpha: Target) -> None:
    c = alpha.client()
    c.login()
    r = c.get("/search")
    assert r.status == 200 and "Member Search" in r.body and "Member ID" in r.body and ">Search<" in r.body
    assert "Sign out" in r.body
    r = c.get(f"/search?member_id={SECOND}")
    assert r.status == 302 and r.location.endswith(f"/member/{SECOND}")
    last = next(m for m in seeded_members() if m["member_id"] == CANONICAL)["last_name"]
    r = c.get(f"/search?last_name={last}")
    assert r.status == 200 and f'/member/{CANONICAL}"' in r.body


def test_pane_pinned_labels_ssn_dob_accounts_and_link(alpha: Target) -> None:
    c = alpha.client()
    c.login()
    m = next(m for m in seeded_members() if m["member_id"] == CANONICAL)
    r = c.get(f"/member/{CANONICAL}/pane")
    assert r.status == 200
    for s in (">Date of Birth<", m["dob"], ">SSN<", m["ssn"], ">Open Sub-Account<", 'target="_top"'):
        assert s in r.body, s
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", m["dob"])
    # Header order on alpha: Type | Account No. | Balance; exactly one "Savings" row.
    assert r.body.index("Type") < r.body.index("Account No.") < r.body.index("Balance")
    savings = next(a for a in m["accounts"] if a["type"] == "Savings")
    assert f"${savings['balance']:,.2f}" in r.body
    assert len(re.findall(r">Savings<", r.body)) == 1


def test_not_found_page_has_no_heading_and_no_iframe(alpha: Target) -> None:
    c = alpha.client()
    c.login()
    r = c.get("/member/999999")
    assert r.status == 200 and "No member found for 999999" in r.body
    assert "Member Profile" not in r.body and "<iframe" not in r.body


def test_subaccount_flow_validation_review_confirm_and_state(alpha: Target) -> None:
    c = alpha.client()
    c.login()
    base = f"/member/{CANONICAL}/subaccount"
    r = c.get(f"{base}/new")
    assert r.status == 200 and "novalidate" in r.body and "Continue to review" in r.body
    assert " min=" not in r.body and " required" not in r.body
    r = c.post(f"{base}/new", {"account_type": "Savings", "nickname": "Holiday fund", "initial_deposit": "10", "terms": "on"})
    assert r.status == 200
    assert "Please correct the errors below." in r.body and "Initial deposit must be at least $25." in r.body
    assert 'class="cell err"' in r.body and r.body.count('<span class="err">') == 1  # banner + exactly one inline error: the $25 rule is the only rule
    r = c.post(f"{base}/new", {"account_type": "Savings", "nickname": "", "initial_deposit": "nan", "terms": ""})
    assert r.status == 200 and "Initial deposit must be at least $25." in r.body
    before = json.loads(c.get(f"/__state/member/{CANONICAL}").body)["runtime_created"]
    r = c.post(f"{base}/new", {"account_type": "Savings", "nickname": "Holiday fund", "initial_deposit": "50", "terms": "on"})
    assert r.status == 302 and r.location.endswith(f"{base}/review")
    r = c.get(f"{base}/review")
    assert r.status == 200 and "Review new sub-account" in r.body and "Confirm and open account" in r.body
    assert json.loads(c.get(f"/__state/member/{CANONICAL}").body)["runtime_created"] == before  # nothing created yet
    r = c.post(f"{base}/review", {})
    assert r.status == 302 and r.location.endswith(f"{base}/confirmation")
    r = c.get(f"{base}/confirmation")
    assert r.status == 200 and "Sub-account opened" in r.body and ">Account Number<" in r.body
    state = json.loads(c.get(f"/__state/member/{CANONICAL}").body)
    assert len(state["runtime_created"]) == len(before) + 1
    acct = state["runtime_created"][-1]
    assert acct["account_no"] in r.body and acct["nickname"] == "Holiday fund" and acct["balance"] == 50.0
    assert "Savings (sub)" in c.get(f"/member/{CANONICAL}/pane").body


def test_sign_out_link_clears_the_session(alpha: Target) -> None:
    c = alpha.client()
    c.login()
    assert "Sign out" in c.get("/search").body
    r = c.get("/logout")
    assert r.status == 302 and r.location.endswith("/login")
    assert c.get("/search").status == 302


def test_inject_expire_fires_once_on_first_authenticated_member_get(target_factory) -> None:
    t = target_factory(tenant="alpha", inject=("expire",))
    c = t.client()
    c.login()
    assert c.get("/search").status == 200  # not a member GET: nothing fires
    r = c.get(f"/member/{CANONICAL}")
    assert r.status == 302 and r.location.endswith(f"/login?next=/member/{CANONICAL}&reason=expired")
    assert c.get("/search").status == 302  # session really cleared
    assert "session has expired" in c.get(f"/login?next=/member/{CANONICAL}&reason=expired").body
    assert c.login(next_path=f"/member/{CANONICAL}").location.endswith(f"/member/{CANONICAL}")
    assert c.get(f"/member/{CANONICAL}").status == 200  # once


def test_inject_modal_fires_once_and_query_param_arms(target_factory) -> None:
    t = target_factory(tenant="alpha", inject=("modal",))
    c = t.client()
    c.login()
    r = c.get("/search")
    assert "Scheduled maintenance notice" in r.body and ">Dismiss<" in r.body and "alert(" not in r.body
    assert "Scheduled maintenance notice" not in c.get("/search").body  # once
    # ?inject= arms the same one-shot injection on a clean server.
    t2 = target_factory(tenant="alpha")
    c2 = t2.client()
    c2.login()
    assert "Scheduled maintenance notice" not in c2.get("/search").body
    assert "Scheduled maintenance notice" in c2.get("/search?inject=modal").body
    assert "Scheduled maintenance notice" not in c2.get("/search").body


def test_inject_slow_delays_the_shell_only(target_factory) -> None:
    t = target_factory(tenant="alpha", inject=("slow",), slow_ms=600)
    c = t.client()
    c.login()
    t0 = time.monotonic()
    assert c.get(f"/member/{CANONICAL}").status == 200
    shell = time.monotonic() - t0
    t0 = time.monotonic()
    assert c.get(f"/member/{CANONICAL}/pane").status == 200
    pane = time.monotonic() - t0
    assert shell >= 0.6 and pane < 0.5
    t0 = time.monotonic()
    c.get(f"/member/{CANONICAL}")
    assert time.monotonic() - t0 >= 0.6  # every time


def test_inject_deny_is_sticky_on_the_confirm_post(target_factory) -> None:
    t = target_factory(tenant="alpha", inject=("deny",))
    c = t.client()
    c.login()
    base = f"/member/{CANONICAL}/subaccount"
    c.post(f"{base}/new", {"account_type": "Savings", "nickname": "x", "initial_deposit": "50", "terms": "on"})
    for _ in range(2):
        r = c.post(f"{base}/review", {})
        assert r.status == 200 and "You do not have permission to open sub-accounts." in r.body
    assert json.loads(c.get(f"/__state/member/{CANONICAL}").body)["runtime_created"] == []


def test_inject_err500_is_sticky_on_the_subaccount_form(target_factory) -> None:
    t = target_factory(tenant="alpha", inject=("err500",))
    c = t.client()
    c.login()
    assert c.get(f"/member/{CANONICAL}").status == 200
    for _ in range(2):
        r = c.get(f"/member/{CANONICAL}/subaccount/new")
        assert r.status == 500 and "<title>500 Internal Server Error</title>" in r.body


def test_beta_prefix_labels_column_order_and_plain_404(beta: Target) -> None:
    c = beta.client()
    r = c.get("/search")
    assert r.status == 404 and "Location" not in r.headers  # unprefixed: plain 404, never a redirect
    r = c.get("/portal/search")
    assert r.status == 302 and r.location.endswith("/portal/login?next=/portal/search")
    assert c.login(prefix="/portal").location.endswith("/portal/search")
    r = c.get("/portal/search")
    assert "Customer Number" in r.body and "Member ID" not in r.body and "Sign out" in r.body
    r = c.get(f"/portal/member/{CANONICAL}/pane")
    assert "Add Product" in r.body and "Open Sub-Account" not in r.body
    assert r.body.index("Account No.") < r.body.index("Balance") < r.body.index("Type")
    assert ">SSN<" in r.body and ">Date of Birth<" in r.body
    assert c.get("/portal/static/console.css").status == 200  # static lives under the prefix too
    assert c.get("/static/console.css").status == 404
    base = f"/portal/member/{CANONICAL}/subaccount"
    c.post(f"{base}/new", {"account_type": "Savings", "nickname": "x", "initial_deposit": "50", "terms": "on"})
    r = c.get(f"{base}/review")
    assert r.status == 200 and ">Complete<" in r.body and "Confirm and open account" not in r.body


def test_debug_state_is_absent_without_the_flag(target_factory) -> None:
    t = target_factory(tenant="alpha", debug_state=False)
    c = t.client()
    c.login()
    assert c.get(f"/__state/member/{CANONICAL}").status == 404
