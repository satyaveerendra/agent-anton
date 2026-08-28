"""Policy configuration and — from phase 3 — the ActionGate, LeaseState and ApprovalToken.

Phase 1 ships only ``PolicyConfig`` / ``load_policy`` (SPEC §8) because the loader rules
in ``anton.schema.loader`` need ``forbidden_extract_patterns`` and ``allowed_url_patterns``.
The single enforcement choke point (``ActionGate.check``) lands with the surface in phase 3.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from anton.schema.artifact import ActionType, StrictModel

__all__ = ["RiskyRule", "PolicyConfig", "load_policy", "expand_env", "DEFAULT_POLICY_PATH"]

DEFAULT_POLICY_PATH = Path(__file__).with_name("policy.yaml")

_ENV_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


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
