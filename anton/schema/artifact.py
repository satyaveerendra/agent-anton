"""Capability artifact schema — the centerpiece (SPEC §4).

The artifact is a **capability contract**, not a macro. A calling agent must be able to
read it and know what state it needs (``preconditions`` / ``requires_flows``), what to send
(``inputs``), what comes back (``outputs``), and every outcome it can get
(``business_outcomes`` — the closed outcome space). A human reviewer must be able to audit
every step, every locator (with its ``rationale``), and every declared outcome. It is fully
decoupled from the model transcript: nothing here references a run, a turn or a ref.

Every polymorphic field is a discriminated union (``kind`` / ``arm``), never a bare dict and
never optional-fields-plus-a-kind, so ``CapabilityArtifact.model_json_schema()`` is itself
part of the reviewable deliverable.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"

#: ``$param:<name>`` template token, legal inside locator strings, fingerprint snippets and
#: regex predicates. The engine expands it with the caller's ORIGINAL param string.
PARAM_TOKEN = re.compile(r"\$param:([A-Za-z_][A-Za-z0-9_]*)")

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_.]*$")
STEP_ID = re.compile(r"^[a-z][a-z0-9_]*$")

#: Pseudo-step ids the engine uses in results; a StepSpec may never use them.
RESERVED_STEP_IDS = frozenset({"__preconditions__", "__success__", "__outputs__"})


def referenced_param_names(data: object) -> set[str]:
    """Every param an artifact fragment references: ``ParamRef`` objects and ``$param:`` tokens
    in any string (locator names, anchors, fingerprint snippets, regexes)."""
    found: set[str] = set()
    if isinstance(data, dict):
        if data.get("kind") == "param" and isinstance(data.get("name"), str):
            found.add(data["name"])
        for v in data.values():
            found |= referenced_param_names(v)
    elif isinstance(data, list):
        for v in data:
            found |= referenced_param_names(v)
    elif isinstance(data, str):
        found.update(PARAM_TOKEN.findall(data))
    return found


class StrictModel(BaseModel):
    """Base for every serialized structure: unknown fields are an error, not noise."""

    model_config = ConfigDict(extra="forbid")


# --- enums -------------------------------------------------------------------------------


class ParamType(StrEnum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"


class OutputType(StrEnum):
    """No secret outputs, ever."""

    STRING = "STRING"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"


class LocatorTier(IntEnum):
    SEMANTIC = 1
    ANCHORED = 2
    VISUAL = 3


class ActionType(StrEnum):
    NAVIGATE = "NAVIGATE"
    CLICK = "CLICK"
    FILL = "FILL"
    SELECT = "SELECT"
    PRESS = "PRESS"
    #: Discovery-only tool action: it passes the gate, but artifacts never contain READ steps.
    READ = "READ"


class RiskClass(StrEnum):
    SAFE = "SAFE"
    RISKY = "RISKY"


# --- params / outputs --------------------------------------------------------------------


class ParamSpec(StrictModel):
    name: str
    type: ParamType
    description: str
    required: bool = True
    pattern: str | None = None
    enum_values: list[str] | None = None
    #: Sensitive values are redacted in all logs/evidence.
    sensitive: bool = False

    @model_validator(mode="after")
    def _enum_needs_values(self) -> ParamSpec:
        if self.type is ParamType.ENUM and not self.enum_values:
            raise ValueError(f"param {self.name!r}: ENUM requires enum_values")
        if self.pattern is not None:
            re.compile(self.pattern)
        return self


# --- locators: a typed union, one class per kind, tier fixed by kind ---------------------


class _LocatorBase(StrictModel):
    tier: LocatorTier
    #: REQUIRED: why this locator is expected to be robust.
    rationale: str = Field(min_length=1)
    #: e.g. ["iframe[name=detail]"]; [] = top frame.
    frame_path: list[str] = Field(default_factory=list)

    _expected_tier: ClassVar[LocatorTier]  # set by subclasses

    @model_validator(mode="after")
    def _tier_matches_kind(self) -> _LocatorBase:
        expected = type(self)._expected_tier
        if self.tier != expected:
            raise ValueError(
                f"locator kind {getattr(self, 'kind', '?')!r} must be tier {int(expected)},"
                f" got {int(self.tier)}"
            )
        return self


class RoleNameLocator(_LocatorBase):
    """Tier 1: computed role + accessible name (frame-aware)."""

    kind: Literal["role_name"] = "role_name"
    role: str
    name: str
    exact: bool = False
    _expected_tier = LocatorTier.SEMANTIC


class LabelRelativeLocator(_LocatorBase):
    """Tier 2: a relation computed over snapshot geometry only — never DOM relations."""

    kind: Literal["label_relative"] = "label_relative"
    relation: Literal["right_of_label", "below_label", "row_cell"]
    #: Label text; for row_cell: text of ANY cell in the row.
    anchor_text: str
    #: row_cell only: restrict the anchor match to this column.
    anchor_col_header: str | None = None
    #: row_cell only: the column header of the cell to return.
    col_header: str | None = None
    #: Optional filter on the candidate's computed role.
    target_role: str | None = None
    _expected_tier = LocatorTier.ANCHORED

    @model_validator(mode="after")
    def _row_cell_fields(self) -> LabelRelativeLocator:
        if self.relation == "row_cell" and not self.col_header:
            raise ValueError("row_cell requires col_header")
        if self.relation != "row_cell" and (self.col_header or self.anchor_col_header):
            raise ValueError("col_header / anchor_col_header are row_cell-only")
        return self


class ExactTextLocator(_LocatorBase):
    """Tier 2: an element whose visible text equals ``text``."""

    kind: Literal["exact_text"] = "exact_text"
    text: str
    _expected_tier = LocatorTier.ANCHORED


class BBoxLocator(_LocatorBase):
    """Tier 3: normalized 0..1 to the top page viewport → element at point."""

    kind: Literal["bbox"] = "bbox"
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    _expected_tier = LocatorTier.VISUAL


LocatorCandidate = Annotated[
    RoleNameLocator | LabelRelativeLocator | ExactTextLocator | BBoxLocator,
    Field(discriminator="kind"),
]


class ElementFingerprint(StrictModel):
    """Sanity check before acting on a resolved element.

    Checked per tier: tiers 1–2 compare role + control_type + name_snippet (+ text_snippet
    when set); tier 3 compares role + control_type only, so it genuinely survives label
    drift and sets ``drift_flag``. ``None`` is a wildcard. Snippets are NEVER set for
    extraction targets or mask ladders (a balance cell's "name" is its value).
    """

    role: str | None = None
    name_snippet: str | None = None
    #: Web: HTML tag; desktop: AX control type.
    control_type: str
    text_snippet: str | None = None


class LocatorLadder(StrictModel):
    #: Ordered; tiers non-decreasing.
    candidates: list[LocatorCandidate] = Field(min_length=1)
    fingerprint: ElementFingerprint

    @model_validator(mode="after")
    def _tiers_non_decreasing(self) -> LocatorLadder:
        tiers = [int(c.tier) for c in self.candidates]
        if tiers != sorted(tiers):
            raise ValueError(f"ladder tiers must be non-decreasing, got {tiers}")
        return self


# --- values: a typed union ---------------------------------------------------------------


class LiteralValue(StrictModel):
    kind: Literal["literal"] = "literal"
    value: str


class ParamRef(StrictModel):
    kind: Literal["param"] = "param"
    name: str


class SecretRef(StrictModel):
    """Resolved from env ``ANTON_SECRET_<NAME>`` at act-time; the value is NEVER stored."""

    kind: Literal["secret"] = "secret"
    name: str


ValueRef = Annotated[LiteralValue | ParamRef | SecretRef, Field(discriminator="kind")]


# --- predicates / signatures: a typed union on kind --------------------------------------


class UrlMatches(StrictModel):
    """Compares the URL *path* after stripping the active ``route_prefix``."""

    kind: Literal["url_matches"] = "url_matches"
    regex: str


class TitleMatches(StrictModel):
    kind: Literal["title_matches"] = "title_matches"
    regex: str


class TextVisible(StrictModel):
    """Over visible static text (SPEC §7)."""

    kind: Literal["text_visible"] = "text_visible"
    regex: str


class ElementVisible(StrictModel):
    kind: Literal["element_visible"] = "element_visible"
    ladder: LocatorLadder


class FieldValue(StrictModel):
    """With a SecretRef ``expected`` asserts only that the field is non-empty (the secret is
    never read back); with a ParamRef/LiteralValue asserts equality against the live value."""

    kind: Literal["field_value"] = "field_value"
    ladder: LocatorLadder
    expected: ValueRef


class Checked(StrictModel):
    kind: Literal["checked"] = "checked"
    ladder: LocatorLadder
    expected: bool = True


class AllOf(StrictModel):
    """Conjunction — for detector ``when`` (which is any_of)."""

    kind: Literal["all_of"] = "all_of"
    predicates: list[Predicate] = Field(min_length=1)


Predicate = Annotated[
    UrlMatches | TitleMatches | TextVisible | ElementVisible | FieldValue | Checked | AllOf,
    Field(discriminator="kind"),
]
AllOf.model_rebuild()


class StateSignature(StrictModel):
    """Checkpoints are signatures, not "hope the click worked"."""

    all_of: list[Predicate] = Field(min_length=1)


# --- recoveries and detectors: typed unions; payload presence enforced by type -----------


class Dismiss(StrictModel):
    kind: Literal["dismiss"] = "dismiss"
    target: LocatorLadder
    #: Times this recovery may execute for ONE step within ONE run.
    max_attempts: int = Field(default=2, ge=1)


class RetryStep(StrictModel):
    kind: Literal["retry_step"] = "retry_step"
    max_attempts: int = Field(default=2, ge=1)


class RunFlow(StrictModel):
    kind: Literal["run_flow"] = "run_flow"
    #: Flow defined in ``AppProfile.flows``.
    flow_id: str
    max_attempts: int = Field(default=2, ge=1)


class Escalate(StrictModel):
    kind: Literal["escalate"] = "escalate"


RecoveryAction = Annotated[Dismiss | RetryStep | RunFlow | Escalate, Field(discriminator="kind")]


class BusinessDetector(StrictModel):
    arm: Literal["BUSINESS_OUTCOME"] = "BUSINESS_OUTCOME"
    id: str
    #: any_of semantics; wrap in AllOf for a conjunction.
    when: list[Predicate] = Field(min_length=1)
    #: e.g. "MEMBER_NOT_FOUND".
    outcome_code: str
    #: Element whose LIVE text becomes ``ReplayResult.outcome_detail`` (then redacted).
    detail_locator: LocatorLadder | None = None


class RecoverableDetector(StrictModel):
    arm: Literal["RECOVERABLE"] = "RECOVERABLE"
    id: str
    when: list[Predicate] = Field(min_length=1)
    recovery: RecoveryAction


class HardFailureDetector(StrictModel):
    arm: Literal["HARD_FAILURE"] = "HARD_FAILURE"
    id: str
    when: list[Predicate] = Field(min_length=1)
    reason: str


OutcomeDetector = Annotated[
    BusinessDetector | RecoverableDetector | HardFailureDetector, Field(discriminator="arm")
]


# --- steps --------------------------------------------------------------------------------


class StepSpec(StrictModel):
    #: Generated by the SPEC §10.3 id rule; hand-authored artifacts follow it too.
    id: str = Field(pattern=STEP_ID.pattern)
    description: str
    action: ActionType
    #: None for NAVIGATE / PRESS-global.
    target: LocatorLadder | None = None
    value: ValueRef | None = None
    #: Effective risk at replay = max(policy match, this): artifacts may RAISE, never lower.
    risk: RiskClass = RiskClass.SAFE
    #: Derived: always == (risk == RISKY).
    requires_approval: bool = False
    #: Per-step checkpoint — every step has one.
    postcondition: StateSignature
    wait_timeout_ms: int = Field(default=8000, ge=250)
    #: Extra resolve→act→wait cycles when the postcondition times out with NO detector firing.
    retries: int = Field(default=1, ge=0)
    #: Step-scoped; evaluated every poll tick alongside the postcondition (SPEC §9).
    detectors: list[OutcomeDetector] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> StepSpec:
        if self.id in RESERVED_STEP_IDS:
            raise ValueError(f"step id {self.id!r} is reserved")
        if self.action is ActionType.READ:
            raise ValueError("READ is a discovery-only tool action; artifacts never contain READ steps")
        risky = self.risk is RiskClass.RISKY
        self.requires_approval = risky  # derived: forced from risk, never trusted from input
        if risky:
            if self.retries != 0:
                raise ValueError("a RISKY step must have retries == 0 (re-clicking Confirm on an ambiguous state is the duplicate-transaction bug)")
            for d in self.detectors:
                if isinstance(d, RecoverableDetector) and isinstance(d.recovery, RetryStep):
                    raise ValueError("a RISKY step may not carry a RetryStep recovery")
        if self.action in (ActionType.CLICK, ActionType.FILL, ActionType.SELECT) and self.target is None:
            raise ValueError(f"{self.action.value} requires a target")
        if self.action in (ActionType.FILL, ActionType.SELECT):
            if self.value is None:
                raise ValueError(f"{self.action.value} requires a value")
            if not any(
                isinstance(p, (FieldValue, Checked)) and p.ladder == self.target
                for p in self.postcondition.all_of
            ):
                raise ValueError(f"{self.action.value} steps must carry a FieldValue/Checked predicate on their target")
        if self.action is ActionType.NAVIGATE and self.value is None:
            raise ValueError("NAVIGATE requires a URL value")
        return self


# --- provenance / approval / artifact ----------------------------------------------------


class RecordedAgainst(StrictModel):
    app_id: str
    tenant_id: str
    app_version: str | None = None
    entry_url: str


class Provenance(StrictModel):
    origin: Literal["discovery", "hand_authored"]
    discovery_run_id: str | None = None
    model: str | None = None
    created_at: datetime
    surface: str = "web"
    recorded_against: RecordedAgainst

    @model_validator(mode="after")
    def _discovery_has_run(self) -> Provenance:
        if self.origin == "discovery" and not self.discovery_run_id:
            raise ValueError("origin=discovery requires discovery_run_id")
        return self


class Approval(StrictModel):
    approved_at: datetime
    approved_by: str
    #: Content hash (see ``content_hash``).
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapabilityArtifact(StrictModel):
    schema_version: str = SCHEMA_VERSION
    #: e.g. "servicing.read_savings_balance".
    capability_id: str = Field(pattern=CAPABILITY_ID.pattern)
    name: str
    description: str
    #: semver, starts "1.0.0".
    version: str = Field(pattern=SEMVER.pattern)
    status: Literal["draft", "approved"] = "draft"
    #: status == "approved" ⇒ approval present; the hash is checked by the loader.
    approval: Approval | None = None
    #: e.g. "profiles/servicing.yaml" (repo-root relative).
    app_profile: str
    #: AppProfile flow ids the engine runs, in order, when preconditions do not hold.
    requires_flows: list[str] = Field(default_factory=lambda: ["login"])
    #: State required before step 1 (e.g. authenticated, at the search page).
    preconditions: StateSignature
    inputs: list[ParamSpec]
    outputs: list[OutputSpec]
    #: code -> human meaning: the CLOSED outcome space (closure enforced by the loader).
    business_outcomes: dict[str, str]
    steps: list[StepSpec] = Field(min_length=1)
    #: Final checkpoint; outputs extracted only after it holds.
    success: StateSignature
    provenance: Provenance

    @model_validator(mode="after")
    def _rules(self) -> CapabilityArtifact:
        if self.status == "approved" and self.approval is None:
            raise ValueError("status=approved requires an approval block")
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step ids must be unique")
        names = [p.name for p in self.inputs]
        if len(set(names)) != len(names):
            raise ValueError("input names must be unique")
        onames = [o.name for o in self.outputs]
        if len(set(onames)) != len(onames):
            raise ValueError("output names must be unique")
        referenced = referenced_param_names(self.model_dump(mode="json", exclude={"inputs"}))
        unknown = referenced - set(names)
        if unknown:
            raise ValueError(f"steps reference undeclared params: {sorted(unknown)}")
        for code in self.business_outcomes:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code):
                raise ValueError(f"outcome code {code!r} must be UPPER_SNAKE")
        return self

    def risky_steps(self) -> list[StepSpec]:
        return [s for s in self.steps if s.risk is RiskClass.RISKY]


class ExtractionSpec(StrictModel):
    locator: LocatorLadder
    attr: Literal["text", "value"]
    #: Runs first; group 1 if present, else whole match.
    post_process_regex: str | None = None
    #: Runs second; "money" strips $ , and whitespace.
    normalize: Literal["none", "trim", "money", "digits"] = "trim"

    @model_validator(mode="after")
    def _regex_compiles(self) -> ExtractionSpec:
        if self.post_process_regex is not None:
            re.compile(self.post_process_regex)
        return self


class OutputSpec(StrictModel):
    name: str
    type: OutputType
    description: str
    sensitive: bool = False
    enum_values: list[str] | None = None
    #: Values are read from the LIVE element (SPEC §9), coerced to ``type`` after normalize;
    #: coercion failure is a FAILED result.
    extraction: ExtractionSpec

    @model_validator(mode="after")
    def _enum_needs_values(self) -> OutputSpec:
        if self.type is OutputType.ENUM and not self.enum_values:
            raise ValueError(f"output {self.name!r}: ENUM requires enum_values")
        return self


CapabilityArtifact.model_rebuild()


# --- content hash --------------------------------------------------------------------------


def canonical_json(model: BaseModel) -> str:
    """Sorted keys, no indent, JSON mode — the one serialization every hash uses."""
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def content_hash(artifact: CapabilityArtifact) -> str:
    """sha256 of the artifact with ``status="draft"`` and ``approval=None`` (SPEC §4).

    Approving therefore never moves the hash, and any later edit invalidates it.
    """
    neutral = artifact.model_copy(update={"status": "draft", "approval": None})
    return hashlib.sha256(canonical_json(neutral).encode("utf-8")).hexdigest()


def param_tokens(text: str) -> list[str]:
    """Names referenced by ``$param:<name>`` tokens in ``text``."""
    return PARAM_TOKEN.findall(text)
