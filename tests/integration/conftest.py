"""Integration fixtures (SPEC §16 "Surface", `make verify-surface`): a real Chromium page, the
real target app, the real PlaywrightSurface — headless is fine here."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from anton.policy.gate import load_policy
from anton.policy.redact import Redactor
from anton.schema.app_profile import MaskSpec
from anton.schema.artifact import (
    ActionType,
    ElementFingerprint,
    LabelRelativeLocator,
    LocatorLadder,
    LocatorTier,
    RoleNameLocator,
)
from anton.surface.base import ConcreteAction, ResolvedElement
from anton.surface.web import PlaywrightSurface
from tests.acceptance.conftest import CREDS, Target, spawn_target

PANE = ["iframe[name=detail]"]


def mask(mask_id: str, label: str, applies_to: str, frame: list[str]) -> MaskSpec:
    return MaskSpec(
        id=mask_id,
        ladder=LocatorLadder(
            candidates=[LabelRelativeLocator(tier=LocatorTier.ANCHORED, relation="right_of_label", anchor_text=label, rationale="value cell right of the label", frame_path=frame)],
            fingerprint=ElementFingerprint(role="cell", control_type="td"),
        ),
        applies_to=applies_to,
    )


#: The three masks SPEC §4 says profiles/servicing.yaml pins.
MASKS = [
    mask("ssn_cell", "SSN", r"^/member/[^/]+$", PANE),
    mask("dob_cell", "Date of Birth", r"^/member/[^/]+$", PANE),
    mask("account_number_cell", "Account Number", r"^/member/[^/]+/subaccount/confirmation$", []),
]


@pytest.fixture(scope="session")
def alpha() -> Iterator[Target]:
    t = spawn_target("alpha")
    try:
        yield t
    finally:
        t.stop()


@pytest.fixture(scope="session")
def beta() -> Iterator[Target]:
    t = spawn_target("beta")
    try:
        yield t
    finally:
        t.stop()


@pytest.fixture
def target_factory() -> Iterator[Callable[..., Target]]:
    """Short-lived target apps (one per injection), stopped on teardown."""
    spawned: list[Target] = []

    def make(**kw) -> Target:  # noqa: ANN003 - passthrough to spawn_target
        t = spawn_target(**kw)
        spawned.append(t)
        return t

    try:
        yield make
    finally:
        for t in spawned:
            t.stop()


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    try:
        yield pg
    finally:
        context.close()


@pytest.fixture
def events() -> list[tuple[str, dict]]:
    return []


@pytest.fixture
def make_surface(page: Page, events: list, tmp_path: Path) -> Callable[..., PlaywrightSurface]:
    def make(route_prefix: str = "", masks: list[MaskSpec] | None = None) -> PlaywrightSurface:
        policy = load_policy(env={"ANTON_TARGET_ORIGIN": "http://127.0.0.1:1"})
        redactor = Redactor(policy.redact_field_patterns, policy.redact_value_patterns)
        return PlaywrightSurface(
            page, redactor, sensitive_elements=MASKS if masks is None else masks, route_prefix=route_prefix,
            screenshot_dir=tmp_path / "shots", on_event=lambda k, d: events.append((k, d)),
        )

    return make


def rn(role: str, name: str, frame: list[str] | None = None, exact: bool = False) -> RoleNameLocator:
    return RoleNameLocator(tier=LocatorTier.SEMANTIC, role=role, name=name, exact=exact, rationale="test", frame_path=frame or [])


def must(surface: PlaywrightSurface, cand: RoleNameLocator | LabelRelativeLocator) -> ResolvedElement:
    el = surface.resolve(cand)
    assert el is not None, f"could not resolve {cand!r}"
    return el


def login_via_surface(surface: PlaywrightSurface, target: Target, next_path: str | None = None) -> None:
    """Drive the real login form through the surface only (NAVIGATE / FILL / CLICK)."""
    url = f"{target.origin}{target.prefix}/login" + (f"?next={next_path}" if next_path else "")
    surface.act(ConcreteAction(ActionType.NAVIGATE, url=url), 8000)
    surface.page.wait_for_load_state()
    surface.observe()
    surface.act(ConcreteAction(ActionType.FILL, target=must(surface, rn("textbox", "Username")), value=CREDS["username"], value_kind="literal"), 8000)
    surface.act(ConcreteAction(ActionType.FILL, target=must(surface, rn("textbox", "Password")), value=CREDS["password"], value_kind="secret"), 8000)
    click_and_wait(surface, must(surface, rn("button", "Sign in")))


def click_and_wait(surface: PlaywrightSurface, target: ResolvedElement, timeout_ms: int = 8000) -> None:
    """act() never waits for the navigation a click starts (the engine polls); tests do."""
    with surface.page.expect_navigation(timeout=timeout_ms):
        surface.act(ConcreteAction(ActionType.CLICK, target=target), timeout_ms)
    surface.page.wait_for_load_state("networkidle")
    surface.observe()
