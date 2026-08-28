"""PlaywrightSurface — the sole Surface implementation (SPEC §7).

Perception: ``observe()`` builds the redacted SemanticSnapshot across frames. Resolution:
``resolve(candidate)`` is pure geometry over that snapshot (``anton.replay.locate``), then the
ref is turned back into a live handle. Action: ``act()`` uses the step budget as the Playwright
timeout (never the 30 s default), never waits for a resulting navigation, and maps every driver
error to ``ActError``. Native dialogs are logged and dismissed. Screenshots apply
``sensitive_elements`` masks by injecting an opaque overlay into the element's own frame and
fail CLOSED (a solid PNG + ``mask_unresolved``) when a required mask does not resolve.
"""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from anton.policy.redact import Redactor
from anton.replay.locate import resolve_candidate
from anton.schema.app_profile import MaskSpec
from anton.schema.artifact import ActionType, LocatorCandidate
from anton.schema.snapshot import BBox, SemanticSnapshot
from anton.surface.base import ActError, ConcreteAction, ResolvedElement
from anton.surface.snapshot import RawSnapshot, build_snapshot

__all__ = ["PlaywrightSurface", "solid_png"]

EventSink = Callable[[str, dict[str, Any]], None]

_MASK_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  const d = document.createElement('div');
  d.setAttribute('data-anton-mask', '1');
  Object.assign(d.style, { position: 'fixed', left: r.left + 'px', top: r.top + 'px', width: r.width + 'px',
    height: r.height + 'px', background: '#000', zIndex: '2147483647', pointerEvents: 'none' });
  document.body.appendChild(d);
}
"""
_UNMASK_JS = "() => { document.querySelectorAll('[data-anton-mask]').forEach(n => n.remove()); }"


def solid_png(width: int, height: int, rgb: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    """A generated solid-colour PNG (stdlib only) — the fail-closed screenshot."""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


class PlaywrightSurface:
    def __init__(
        self,
        page: Any,
        redactor: Redactor,
        sensitive_elements: list[MaskSpec] | None = None,
        route_prefix: str = "",
        *,
        screenshot_dir: Path,
        on_event: EventSink | None = None,
    ) -> None:
        self.page = page
        self.redactor = redactor
        self.sensitive_elements = list(sensitive_elements or [])
        self.route_prefix = route_prefix
        self.screenshot_dir = Path(screenshot_dir)  # always inside the run directory (SPEC §12)
        self._on_event = on_event or (lambda kind, data: None)
        self._snapshot: SemanticSnapshot | None = None
        self._raw: RawSnapshot | None = None
        self._shots = 0
        page.on("dialog", self._on_dialog)

    # --- events ----------------------------------------------------------------------------

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        self._on_event(kind, self.redactor.scrub(data))

    def _on_dialog(self, dialog: Any) -> None:
        self._emit("dialog", {"type": dialog.type, "message": dialog.message, "handled": "dismiss"})
        try:
            dialog.dismiss()
        except PlaywrightError:
            pass

    # --- perception --------------------------------------------------------------------------

    def url(self) -> str:
        return self.page.url

    def path(self) -> str:
        """The current path with the active route_prefix stripped (what url_matches sees)."""
        path = re.sub(r"^https?://[^/]+", "", self.page.url).split("?", 1)[0].split("#", 1)[0] or "/"
        if self.route_prefix and (path == self.route_prefix or path.startswith(self.route_prefix + "/")):
            path = path[len(self.route_prefix):] or "/"
        return path

    def _applicable_masks(self) -> list[MaskSpec]:
        path = self.path()
        return [m for m in self.sensitive_elements if re.search(m.applies_to, path)]

    def _mask_elements(self, raw: RawSnapshot) -> dict[str, str | None]:
        """mask id → ref of the element it resolves to (over the UNREDACTED enumeration), or None."""
        unredacted = SemanticSnapshot(url=raw.url, title=raw.title, frame_count=len(raw.frames), elements=[r.to_element() for r in raw.raw])
        found: dict[str, str | None] = {}
        for mask in self._applicable_masks():
            hit = None
            for cand in mask.ladder.candidates:
                el = resolve_candidate(unredacted, cand, raw.viewport)
                if el is not None:
                    hit = el.ref
                    break
            found[mask.id] = hit
        return found

    def _mask_regions(self, raw: RawSnapshot) -> list[BBox]:
        by_ref = {r.ref: r for r in raw.raw}
        return [by_ref[ref].bbox for ref in self._mask_elements(raw).values() if ref is not None]

    def observe(self) -> SemanticSnapshot:
        snapshot, raw = build_snapshot(self.page, self.redactor, self._mask_regions)
        self._snapshot, self._raw = snapshot, raw
        return snapshot

    def last_snapshot(self) -> SemanticSnapshot:
        return self._ensure()[0]

    def _ensure(self) -> tuple[SemanticSnapshot, RawSnapshot]:
        if self._snapshot is None or self._raw is None:
            self.observe()
        assert self._snapshot is not None and self._raw is not None
        return self._snapshot, self._raw

    def _handle_for(self, raw: RawSnapshot, ref: str) -> Any:
        frame_index, local = raw.locator[ref]
        frame = raw.frames[frame_index].frame
        return frame.evaluate_handle("i => window.__anton_els[i]", local)

    def resolve(self, candidate: LocatorCandidate) -> ResolvedElement | None:
        snapshot, raw = self._ensure()
        el = resolve_candidate(snapshot, candidate, raw.viewport)
        if el is None or el.ref not in raw.locator:
            return None
        live = next((r for r in raw.raw if r.ref == el.ref), None)
        if live is None:
            return None
        try:
            handle = self._handle_for(raw, el.ref)
        except PlaywrightError:
            return None
        return ResolvedElement(
            ref=el.ref, frame_path=list(el.frame_path), bbox=el.bbox, control_type=el.control_type,
            role=live.role, name=live.name, handle=handle, text=el.text,
        )

    def viewport(self) -> tuple[int, int]:
        _, raw = self._ensure()
        return raw.viewport if raw.viewport != (0, 0) else (1280, 900)

    def resolve_ref(self, ref: str) -> ResolvedElement | None:
        """A turn-scoped ref from the LAST observed snapshot → live element (discovery tools)."""
        snapshot, raw = self._ensure()
        el = next((e for e in snapshot.elements if e.ref == ref), None)
        live = next((r for r in raw.raw if r.ref == ref), None)
        if el is None or live is None or ref not in raw.locator:
            return None
        try:
            handle = self._handle_for(raw, ref)
        except PlaywrightError:
            return None
        return ResolvedElement(ref=ref, frame_path=list(el.frame_path), bbox=el.bbox, control_type=el.control_type, role=live.role, name=live.name, handle=handle, text=el.text)

    def snapshot_text(self) -> dict[str, str]:
        """ref → redacted text of the last snapshot (for fingerprint text_snippet checks)."""
        return {e.ref: e.text for e in self._snapshot.elements} if self._snapshot else {}

    # --- action ---------------------------------------------------------------------------------

    def act(self, action: ConcreteAction, timeout_ms: int) -> None:
        a, t = action.action, action.target
        try:
            if a is ActionType.NAVIGATE:
                if not action.url:
                    raise ActError("navigation", "NAVIGATE without a URL")
                self.page.goto(action.url, wait_until="commit", timeout=timeout_ms)
                return
            if a is ActionType.PRESS and t is None:
                self.page.keyboard.press(action.value or "Enter")
                return
            if t is None:
                raise ActError("detached", f"{a.value} needs a resolved target")
            h = t.handle
            if a is ActionType.CLICK:
                h.click(timeout=timeout_ms, no_wait_after=True)
            elif a is ActionType.FILL:
                h.fill(action.value or "", timeout=timeout_ms)
            elif a is ActionType.SELECT:
                # One driver call within the budget: pick label vs value from the live option list.
                wanted = action.value or ""
                by_label = h.evaluate("(el, v) => Array.from(el.options || []).some(o => o.label === v || o.textContent.trim() === v)", wanted)
                if by_label:
                    h.select_option(label=wanted, timeout=timeout_ms)
                else:
                    h.select_option(value=wanted, timeout=timeout_ms)
            elif a is ActionType.PRESS:
                h.press(action.value or "Enter", timeout=timeout_ms, no_wait_after=True)
            elif a is ActionType.READ:
                return
        except ActError:
            raise
        except PlaywrightTimeoutError as e:
            msg = str(e)
            kind: Literal["obscured", "timeout"] = "obscured" if ("intercepts pointer events" in msg or "not visible" in msg or "not receive pointer" in msg) else "timeout"
            raise ActError(kind, msg.splitlines()[0][:300]) from e
        except PlaywrightError as e:
            msg = str(e)
            if "detached" in msg or "not attached" in msg:
                raise ActError("detached", msg.splitlines()[0][:300]) from e
            if "navigat" in msg.lower() or "net::" in msg:
                raise ActError("navigation", msg.splitlines()[0][:300]) from e
            raise ActError("timeout", msg.splitlines()[0][:300]) from e

    def read(self, el: ResolvedElement, attr: Literal["text", "value", "checked"]) -> str:
        """LIVE, unredacted."""
        try:
            if attr == "checked":
                raw = "true" if el.handle.evaluate("el => !!el.checked") else "false"
            elif attr == "value":
                raw = el.handle.evaluate("el => (el.value ?? '')")
            else:
                raw = el.handle.evaluate("el => (el.innerText ?? el.textContent ?? '')")
        except PlaywrightError as e:
            raise ActError("detached", str(e).splitlines()[0][:300]) from e
        return re.sub(r"\s+", " ", str(raw)).strip()

    # --- screenshots -----------------------------------------------------------------------------

    def screenshot(self, masks: list[MaskSpec] | None = None) -> Path:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._shots += 1
        out = self.screenshot_dir / f"shot-{self._shots:03d}.png"
        self.last_masks: dict[str, str | None] = {}
        wanted = list(masks) if masks is not None else self._applicable_masks()
        # Masks are resolved on a fresh, unredacted enumeration of the current page. It is kept
        # local: the observed snapshot/raw pair must stay paired for resolve() (decisions P3-14).
        _, raw = build_snapshot(self.page, self.redactor, None)
        path = self.path()
        resolved: dict[str, str | None] = {}
        unredacted = SemanticSnapshot(url=raw.url, title=raw.title, frame_count=len(raw.frames), elements=[r.to_element() for r in raw.raw])
        for mask in wanted:
            if not re.search(mask.applies_to, path):
                continue
            hit = None
            for cand in mask.ladder.candidates:
                el = resolve_candidate(unredacted, cand, raw.viewport)
                if el is not None:
                    hit = el.ref
                    break
            resolved[mask.id] = hit
        self.last_masks = dict(resolved)
        missing = [mid for mid, ref in resolved.items() if ref is None and next(m for m in wanted if m.id == mid).required]
        if missing:
            for mid in missing:
                self._emit("mask_unresolved", {"mask_id": mid, "url": self.page.url})
            vw, vh = raw.viewport if raw.viewport != (0, 0) else (1280, 720)
            out.write_bytes(solid_png(max(vw, 1), max(vh, 1)))
            return out
        injected: list[Any] = []
        try:
            for _mid, ref in resolved.items():
                if ref is None:
                    continue
                frame_index, _ = raw.locator[ref]
                frame = raw.frames[frame_index].frame
                injected.append(frame)
                frame.evaluate(_MASK_JS, self._handle_for(raw, ref))
            self.page.screenshot(path=str(out), full_page=False)
            self._emit("screenshot", {"path": out.name, "url": self.page.url, "masks": {mid: bool(ref) for mid, ref in resolved.items()}})
        except PlaywrightError as e:
            # Fail closed: a driver error while masking must never yield an unmasked image.
            self._emit("mask_unresolved", {"mask_id": "*", "url": self.page.url, "detail": str(e).splitlines()[0][:200]})
            vw, vh = raw.viewport if raw.viewport != (0, 0) else (1280, 720)
            out.write_bytes(solid_png(max(vw, 1), max(vh, 1)))
        finally:
            for frame in injected:
                try:
                    frame.evaluate(_UNMASK_JS)
                except PlaywrightError:
                    pass
        return out
