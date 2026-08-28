"""The Surface seam (SPEC §7).

Everything upstream — replay, discovery, distiller, escalation — talks only to ``Surface``.
The Playwright class in ``web.py`` is the sole implementation, but this protocol is the
multi-surface answer: a legacy-web or desktop adapter reimplements observe/act/read/resolve/
screenshot while artifacts, the ladder walk (``anton.replay.locate``) and the replay engine
stay untouched. Tier-1/2 locator resolution is computed over ``SemanticSnapshot`` geometry,
so an adapter only has to enumerate elements with roles, names, text and boxes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from anton.schema.app_profile import MaskSpec
from anton.schema.artifact import ActionType, LocatorCandidate
from anton.schema.snapshot import BBox, SemanticSnapshot

__all__ = ["ActError", "ResolvedElement", "ConcreteAction", "Surface"]


class ActError(Exception):
    """Raised by ``Surface.act`` — never a raw driver exception. The engine treats it as a
    postcondition miss at t=0 (SPEC §9)."""

    def __init__(self, kind: Literal["obscured", "detached", "navigation", "timeout"], detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass
class ResolvedElement:
    """A live element (never serialized). ``role``/``name`` are the LIVE, unredacted values —
    for fingerprints and the gate's ``name_matches``."""

    ref: str
    frame_path: list[str]
    bbox: BBox
    control_type: str
    role: str
    name: str
    handle: Any
    #: The element's text as the snapshot recorded it (redacted view) — for ``text_snippet`` fingerprints.
    text: str = ""


@dataclass
class ConcreteAction:
    """The fully resolved action the gate sees and the surface executes. ``value_kind`` tells
    the gate where the value came from (a literal equal to a resolved secret is a policy
    violation; the same string coming from a ``SecretRef`` is the intended path)."""

    action: ActionType
    target: ResolvedElement | None = None
    value: str | None = None
    url: str | None = None
    value_kind: Literal["literal", "param", "secret"] | None = None


class Surface(Protocol):
    def observe(self) -> SemanticSnapshot:
        """Redacted at build (SPEC §8). Returns an EMPTY snapshot during a pending navigation."""
        ...

    def last_snapshot(self) -> SemanticSnapshot:
        """The snapshot ``resolve`` currently works against (observing first if there is none) —
        the engine logs it as the pre-act ``observation`` (SPEC §9)."""
        ...

    def act(self, action: ConcreteAction, timeout_ms: int) -> None:
        """After ActionGate approval. Never waits for a resulting navigation; raises ActError."""
        ...

    def read(self, el: ResolvedElement, attr: Literal["text", "value", "checked"]) -> str:
        """LIVE, unredacted (SPEC §9: extraction, FieldValue, detail_locator). ``checked`` reads
        "true"/"false" for the ``Checked`` predicate."""
        ...

    def screenshot(self, masks: list[MaskSpec]) -> Path:
        """Masks applied inside the element's own frame; fails CLOSED on an unresolved required mask."""
        ...

    def resolve(self, candidate: LocatorCandidate) -> ResolvedElement | None:
        """ONE candidate, against the last observed snapshot."""
        ...

    def resolve_ref(self, ref: str) -> ResolvedElement | None:
        """A turn-scoped ref (``e12``) from the last observed snapshot → live element (discovery)."""
        ...

    def url(self) -> str:
        """The current top-frame URL (for the gate's post-navigation check)."""
        ...

    def path(self) -> str:
        """The current path with the active route_prefix stripped (what ``url_matches`` sees)."""
        ...
