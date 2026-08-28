"""Policy: configuration (SPEC §8), the ActionGate — the single enforcement choke point —
plus ``LeaseState`` / ``ApprovalToken`` (imported by ``escalation/controller.py`` in phase 6).

Every action, in discovery and in replay, passes ``ActionGate.check`` before ``Surface.act``.
``LEASE_NOT_HELD`` is the one refusal that is NOT a policy violation: it is the guard that
makes "who is in control" enforceable rather than advisory.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml
from pydantic import Field, model_validator

from anton.schema.artifact import ActionType, RiskClass, StrictModel
from anton.surface.base import ConcreteAction

__all__ = [
    "RiskyRule",
    "PolicyConfig",
    "load_policy",
    "expand_env",
    "DEFAULT_POLICY_PATH",
    "LeaseState",
    "LEASE_TRANSITIONS",
    "can_transition",
    "ApprovalToken",
    "ApprovalStore",
    "DenyReason",
    "POLICY_VIOLATIONS",
    "Allow",
    "Deny",
    "NeedsApproval",
    "GateDecision",
    "GateContext",
    "ActionGate",
    "secret_env_name",
    "resolve_secret",
    "glob_covers",
    "glob_match",
]

DEFAULT_POLICY_PATH = Path(__file__).with_name("policy.yaml")

_ENV_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# --- configuration -----------------------------------------------------------------------


def expand_env(value: str, env: dict[str, str] | None = None) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in a string."""
    source = os.environ if env is None else env

    def sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in source and source[name] != "":
            return source[name]
        if default is not None:
            return default
        raise ValueError(f"policy references unset environment variable {name!r}")

    return _ENV_TOKEN.sub(sub, value)


class RiskyRule(StrictModel):
    actions: list[ActionType] = Field(min_length=1)
    url_matches: str | None = None
    name_matches: str | None = None

    @model_validator(mode="after")
    def _at_least_one_and_compiles(self) -> RiskyRule:
        if self.url_matches is None and self.name_matches is None:
            raise ValueError("a risky rule needs url_matches and/or name_matches")
        for r in (self.url_matches, self.name_matches):
            if r is not None:
                re.compile(r)
        return self


class PolicyConfig(StrictModel):
    allowed_origins: list[str] = Field(min_length=1)
    #: Glob semantics: "*" matches exactly one path segment, "**" any depth.
    allowed_url_patterns: list[str] = Field(min_length=1)
    allowed_actions: list[ActionType] = Field(min_length=1)
    #: Matched ⇒ RiskClass.RISKY (effective risk = max(policy, step.risk)).
    risky_rules: list[RiskyRule] = Field(default_factory=list)
    redact_field_patterns: list[str] = Field(default_factory=list)
    redact_value_patterns: list[str] = Field(default_factory=list)
    #: No artifact may declare an output extracted from these.
    forbidden_extract_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _regexes_compile(self) -> PolicyConfig:
        for group in (self.redact_field_patterns, self.redact_value_patterns, self.forbidden_extract_patterns):
            for r in group:
                re.compile(r)
        for p in self.allowed_url_patterns:
            if not p.startswith("/"):
                raise ValueError(f"url pattern {p!r} must start with '/'")
        return self


def _expand_tree(node: object, env: dict[str, str] | None) -> object:
    if isinstance(node, str):
        return expand_env(node, env)
    if isinstance(node, list):
        return [_expand_tree(n, env) for n in node]
    if isinstance(node, dict):
        return {k: _expand_tree(v, env) for k, v in node.items()}
    return node


def load_policy(path: str | Path | None = None, env: dict[str, str] | None = None) -> PolicyConfig:
    """Load ``policy.yaml`` (default: the shipped one) with ``${ENV}`` expansion."""
    p = Path(path) if path is not None else DEFAULT_POLICY_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return PolicyConfig.model_validate(_expand_tree(raw, env))


# --- allowlist glob semantics (SPEC §8): "*" = exactly one segment, "**" = any depth --------


def _segments(pattern: str) -> list[str]:
    return [s for s in pattern.split("/") if s != ""]


def glob_covers(policy_pattern: str, other: str) -> bool:
    """True if ``policy_pattern`` covers ``other`` (a concrete path or a narrower pattern).
    ``**`` matches zero or more segments (so ``/member/**`` covers ``/member``); empty segments
    are dropped, so a trailing slash never changes the answer."""

    def rec(ps: list[str], fs: list[str]) -> bool:
        if not ps:
            return not fs
        head = ps[0]
        if head == "**":
            return any(rec(ps[1:], fs[i:]) for i in range(len(fs) + 1))
        if not fs:
            return False
        if fs[0] == "**":
            return False
        if head == "*":
            return rec(ps[1:], fs[1:])
        return head == fs[0] and rec(ps[1:], fs[1:])

    return rec(_segments(policy_pattern), _segments(other))


def glob_match(pattern: str, path: str) -> bool:
    """Segment glob over a runtime path; query and fragment are ignored."""
    return glob_covers(pattern, path.split("?", 1)[0].split("#", 1)[0])


# --- secrets -------------------------------------------------------------------------------


def secret_env_name(name: str) -> str:
    return f"ANTON_SECRET_{name.upper()}"


def resolve_secret(name: str, env: dict[str, str] | None = None) -> str:
    """``SecretRef(name)`` → env ``ANTON_SECRET_<NAME>`` at act-time; the value is never stored."""
    source = os.environ if env is None else env
    key = secret_env_name(name)
    if key not in source or source[key] == "":
        raise KeyError(f"secret {name!r} is not set (expected environment variable {key})")
    return source[key]


# --- lease + approvals ---------------------------------------------------------------------


class LeaseState(StrEnum):
    AGENT_ACTIVE = "AGENT_ACTIVE"
    PAUSED = "PAUSED"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    RESUMING = "RESUMING"
    ABANDONED = "ABANDONED"


#: The full edge list of SPEC §11; anything else is an invalid transition (409 from the console).
LEASE_TRANSITIONS: dict[tuple[LeaseState, LeaseState], str] = {
    (LeaseState.AGENT_ACTIVE, LeaseState.PAUSED): "trigger",
    (LeaseState.PAUSED, LeaseState.HUMAN_ACTIVE): "take",
    (LeaseState.HUMAN_ACTIVE, LeaseState.RESUMING): "handback",
    (LeaseState.RESUMING, LeaseState.AGENT_ACTIVE): "resume",
    (LeaseState.RESUMING, LeaseState.PAUSED): "re-escalate",
    (LeaseState.PAUSED, LeaseState.AGENT_ACTIVE): "approve/deny",
    (LeaseState.PAUSED, LeaseState.ABANDONED): "abort/timeout",
    (LeaseState.HUMAN_ACTIVE, LeaseState.ABANDONED): "abort/timeout",
}


def can_transition(src: LeaseState, dst: LeaseState) -> bool:
    return (src, dst) in LEASE_TRANSITIONS


class ApprovalToken(StrictModel):
    """One-shot operator approval bound to exactly one pending action (SPEC §11)."""

    action_id: str
    issued_by: str
    issued_at: datetime


class ApprovalStore:
    """Grants are consumed by exactly the action they name; a second identical action is
    ``NeedsApproval`` again."""

    def __init__(self) -> None:
        self._tokens: dict[str, ApprovalToken] = {}

    def grant(self, token: ApprovalToken) -> None:
        self._tokens[token.action_id] = token

    def consume(self, action_id: str) -> ApprovalToken | None:
        return self._tokens.pop(action_id, None)

    def pending(self) -> list[str]:
        return sorted(self._tokens)


# --- decisions ------------------------------------------------------------------------------


class DenyReason(StrEnum):
    ORIGIN = "ORIGIN"
    URL_PATTERN = "URL_PATTERN"
    ACTION_TYPE = "ACTION_TYPE"
    SECRET_LITERAL = "SECRET_LITERAL"
    POST_NAV_OFFLIST = "POST_NAV_OFFLIST"
    DRAFT_NOT_ALLOWED = "DRAFT_NOT_ALLOWED"
    DRAFT_RISKY = "DRAFT_RISKY"
    #: A SecretRef typed into a field that is not a credential field (decisions A13 / P5-15).
    SECRET_TARGET = "SECRET_TARGET"
    #: NOT a policy violation: the "who is in control" guard.
    LEASE_NOT_HELD = "LEASE_NOT_HELD"


POLICY_VIOLATIONS = frozenset(r for r in DenyReason if r is not DenyReason.LEASE_NOT_HELD)


@dataclass(frozen=True)
class Allow:
    risk: RiskClass
    #: "caller" (--approve-risky) or "operator" (ApprovalToken) when a RISKY action was consented to.
    approval_by: str | None = None
    rule: int | None = None

    @property
    def kind(self) -> str:
        return "Allow"


@dataclass(frozen=True)
class Deny:
    reason: DenyReason
    detail: str

    @property
    def kind(self) -> str:
        return "Deny"

    @property
    def is_policy_violation(self) -> bool:
        return self.reason in POLICY_VIOLATIONS


@dataclass(frozen=True)
class NeedsApproval:
    detail: str
    action_id: str
    rule: int | None = None

    @property
    def kind(self) -> str:
        return "NeedsApproval"


GateDecision = Allow | Deny | NeedsApproval


@dataclass
class GateContext:
    """What the gate needs beyond the action itself (SPEC §8)."""

    current_url: str
    lease_state: LeaseState
    #: Effective allowlist (policy ceiling ∩ prefixed profile patterns), computed by the loader.
    effective_url_patterns: list[str]
    step_risk: RiskClass = RiskClass.SAFE
    artifact_status: str = "approved"
    approve_risky: bool = False
    #: Identifies the pending action for one-shot operator approvals.
    action_id: str = ""
    approvals: ApprovalStore | None = None
    #: Resolved secret values (for SECRET_LITERAL).
    secret_values: frozenset[str] = field(default_factory=frozenset)
    #: Active tenant prefix; risky-rule ``url_matches`` sees the prefix-stripped path (§4 UrlMatches).
    route_prefix: str = ""


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _path(url: str, route_prefix: str = "") -> str:
    path = urlsplit(url).path or "/"
    if route_prefix and path.startswith(route_prefix):
        path = path[len(route_prefix):] or "/"
    return path


class ActionGate:
    """``check(action, ctx) -> Allow | Deny | NeedsApproval`` — the single choke point."""

    def __init__(self, policy: PolicyConfig) -> None:
        self.policy = policy
        self._origins = {o.lower().rstrip("/") for o in policy.allowed_origins}
        self._rules = [
            (re.compile(r.url_matches) if r.url_matches else None, re.compile(r.name_matches) if r.name_matches else None, r)
            for r in policy.risky_rules
        ]
        self._credential_rx = [re.compile(p) for p in policy.redact_field_patterns]

    def is_credential_target(self, name: str) -> bool:
        """A target whose accessible name matches a ``redact_field_patterns`` entry ("Password", …)."""
        return any(rx.search(name or "") for rx in self._credential_rx)

    # --- allowlist ---------------------------------------------------------------------------

    def url_check(self, url: str, effective_patterns: list[str]) -> Deny | None:
        if _origin(url) not in self._origins:
            return Deny(DenyReason.ORIGIN, f"origin {_origin(url)} is not in allowed_origins {sorted(self._origins)}")
        path = _path(url)
        if not any(glob_match(p, path) for p in effective_patterns):
            return Deny(DenyReason.URL_PATTERN, f"path {path} is not in the effective allowlist {effective_patterns}")
        return None

    def post_navigation_check(self, url: str, effective_patterns: list[str]) -> Deny | None:
        """Layer 2: after any action that changed the URL. A miss stops the run; the page is
        never observed or screenshotted."""
        deny = self.url_check(url, effective_patterns)
        if deny is None:
            return None
        return Deny(DenyReason.POST_NAV_OFFLIST, f"navigated off the allowlist: {deny.detail}")

    @staticmethod
    def draft_check(artifact_status: str, allow_draft: bool, declared_risky_step: str | None) -> Deny | None:
        """Start-up rules (SPEC §8): draft needs --allow-draft; a draft with a declared RISKY step
        is refused regardless."""
        if artifact_status != "draft":
            return None
        if declared_risky_step is not None:
            return Deny(DenyReason.DRAFT_RISKY, f"draft artifact declares RISKY step {declared_risky_step!r}; approve it first")
        if not allow_draft:
            return Deny(DenyReason.DRAFT_NOT_ALLOWED, "artifact is a draft; pass --allow-draft (RISKY steps stay refused)")
        return None

    # --- risk --------------------------------------------------------------------------------

    def policy_risk(self, action: ConcreteAction, current_url: str, route_prefix: str = "") -> int | None:
        """Index (1-based) of the first risky rule matching the concrete action, else None."""
        url = action.url if action.action is ActionType.NAVIGATE and action.url else current_url
        path = _path(url, route_prefix)
        name = action.target.name if action.target is not None else ""
        for i, (url_rx, name_rx, rule) in enumerate(self._rules, start=1):
            if action.action not in rule.actions:
                continue
            if url_rx is not None and url_rx.search(path) is None:
                continue
            if name_rx is not None and name_rx.search(name) is None:
                continue
            return i
        return None

    def effective_risk(self, action: ConcreteAction, step_risk: RiskClass, current_url: str, route_prefix: str = "") -> tuple[RiskClass, int | None]:
        rule = self.policy_risk(action, current_url, route_prefix)
        if rule is not None or step_risk is RiskClass.RISKY:
            return RiskClass.RISKY, rule
        return RiskClass.SAFE, None

    # --- the choke point ---------------------------------------------------------------------

    def check(self, action: ConcreteAction, ctx: GateContext) -> GateDecision:
        if ctx.lease_state is not LeaseState.AGENT_ACTIVE:
            return Deny(DenyReason.LEASE_NOT_HELD, f"lease is {ctx.lease_state}; agent actions are refused")
        if action.action not in self.policy.allowed_actions:
            return Deny(DenyReason.ACTION_TYPE, f"{action.action.value} is not in allowed_actions")
        if action.action is ActionType.NAVIGATE:
            if not action.url:
                return Deny(DenyReason.URL_PATTERN, "NAVIGATE without a URL")
            # Artifacts carry origin-relative URLs; join them against the current page first.
            action.url = urljoin(ctx.current_url, action.url)
        target_url = action.url if action.action is ActionType.NAVIGATE else ctx.current_url
        deny = self.url_check(target_url or "", ctx.effective_url_patterns)
        if deny is not None:
            return deny
        if (
            action.action in (ActionType.FILL, ActionType.SELECT, ActionType.PRESS)
            and action.value_kind != "secret"
            and action.value is not None
            and action.value in ctx.secret_values
        ):
            return Deny(DenyReason.SECRET_LITERAL, "a literal value equals a resolved secret; use a SecretRef")
        if action.action in (ActionType.FILL, ActionType.SELECT, ActionType.PRESS) and action.value_kind == "secret":
            # A secret only ever goes into a credential-like field: never a search box, a query string, a note.
            if action.target is None or not self.is_credential_target(action.target.name):
                shown = action.target.name if action.target is not None else "(no target)"
                return Deny(DenyReason.SECRET_TARGET, f"a SecretRef may only be typed into a credential field; target {shown!r} does not match redact_field_patterns")
        risk, rule = self.effective_risk(action, ctx.step_risk, ctx.current_url, ctx.route_prefix)
        if risk is RiskClass.SAFE:
            return Allow(RiskClass.SAFE)
        if ctx.artifact_status == "draft":
            return Deny(DenyReason.DRAFT_RISKY, "RISKY action from a draft artifact (effective risk); approve it first")
        if ctx.approvals is not None and ctx.action_id and ctx.approvals.consume(ctx.action_id) is not None:
            return Allow(RiskClass.RISKY, approval_by="operator", rule=rule)
        if ctx.approve_risky:
            return Allow(RiskClass.RISKY, approval_by="caller", rule=rule)
        why = f"policy rule {rule}" if rule is not None else "declared RISKY"
        return NeedsApproval(f"RISKY ({why}); no consent for this invocation", action_id=ctx.action_id, rule=rule)
