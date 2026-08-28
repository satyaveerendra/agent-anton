# SPEC.md — Computer-Use Automation System

This is the build specification for this project. You (Claude Code) will build it
end to end in this repo. Read this whole file before writing any code. Where this spec is
silent, make the simplest defensible choice and record it in `docs/decisions.md` (one line
per decision) so the author can fold it into the report.

Naming: the Python package and CLI are `anton`, the repo is `agent-anton`, and the
demo target is "Member Servicing Console" with tenant variants `alpha` and `beta`. No
invented brand names anywhere.

---

## 0. What we are building (context)

A system that gives an AI agent "hands" on legacy UIs that have no API:

1. **Discovery:** an LLM-driven observe → decide → act loop completes a natural-language
   goal against a live UI once.
2. **Distillation:** the successful run is distilled into a **typed, versioned capability
   artifact** — a contract (params in, outputs out, preconditions, steps, checkpoints, a
   closed outcome space), decoupled from the model transcript.
3. **Deterministic replay:** the artifact re-runs with **no LLM in the loop**, using a
   locator fallback ladder, per-step checkpoints, and an explicit outcome taxonomy whose
   result contract can express every terminal state (success, business outcome, blocked
   on a human, failed) and whose exit code is a pure function of the result.
4. **Escalation:** when stuck or when an irreversible step needs a person, the system
   pauses, hands the **same live session** to a human operator (or asks for a one-shot
   approval), records what they do, and resumes.
5. **Guardrails throughout:** allowlist enforcement at a single choke point, risky-action
   gating, and value-level redaction of sensitive data from artifacts, logs, screenshots,
   and the observations sent to the model.

Priorities, in order: system design (artifact schema + replay
contract are central), correctness of the core loop, robustness/error handling,
escalation, generalization story, safety, code quality, communication. Optimize for those.
Do NOT build scaling infrastructure (queues, DBs, services). Single process + the target
app process. Small, correct, well-argued.

## 1. Hard ground rules

- **Never fabricate evidence.** Everything under `/evidence/` must be produced by actually
  running the system. If a phase can't run (e.g. no API key present), stop and say so.
  Placeholder or hand-written "run logs" are grounds for failing the whole project.
- **No secrets in the repo.** API keys via env only. Ship `.env.example`. Demo app
  credentials are fake and clearly marked as fake.
- **No real sites, no real PII.** The only target is the local demo app. All member data
  is synthetic (use obviously fake SSNs/names; generate with a seed script).
- **Replay and the target app must run with zero API keys.** Only `anton discover` (and
  the optional discovery spike, §17) need `ANTHROPIC_API_KEY`.
- **Typed and linted.** Python 3.11+, full type hints on public functions, `ruff` clean,
  `pydantic` v2 models for every serialized structure. Polymorphic fields are
  discriminated unions, never bare `dict` and never optional-fields-plus-a-kind.
- Keep it small. Prefer stdlib. No ORM, no async framework beyond what Playwright needs.
  Simplicity is an explicit evaluation criterion.

## 2. Stack

- Python 3.11+
- **Playwright (sync API, Chromium, headed by default)** — headed matters: the escalation
  demo has a human driving the same window. The sync API is thread-affine; see §11
  "Concurrency model" for the one rule that follows from that.
- **Pydantic v2** — schema, validation, JSON serialization for artifacts/results/logs.
- **Flask** — (a) the target app (its own process), (b) the tiny operator console, hosted
  **in-process as a daemon thread** of the `anton` run that raised the intervention (§11).
- **anthropic** SDK — discovery loop only. Model id from env `ANTON_MODEL`
  (default `claude-sonnet-5`), never hardcoded elsewhere. Adaptive thinking is the
  default on this model family; do not pass `budget_tokens` or assistant prefill.
- pytest, ruff, pyyaml. Pin all deps in `requirements.txt`.

## 3. Repo layout (exact)

```
/README.md                  # setup + demo path (see §14)
/REPORT.md                  # design write-up, 7 fixed headings (see §15)
/SPEC.md                    # this file
/requirements.txt
/.env.example               # ANTHROPIC_API_KEY=, ANTON_MODEL=claude-sonnet-5, ANTON_TARGET_ORIGIN=,
                            # ANTON_SECRET_DEMO_PASSWORD=demo-pass-1   (fake, see §8 secrets)
/Makefile                   # target, discover, replay, replay-notfound, test (= pytest -m "not integration"),
                            # verify-0 verify-surface verify-target verify-1 verify-1b verify-2 verify-3 verify-4
                            # verify-5a verify-5b verify-6 verify-7 verify-evidence verify-all
/anton/
  __main__.py               # CLI entry: python -m anton <cmd>
  cli.py                    # thin wrapper over anton.api
  api.py                    # invoke(...) -> ReplayResult — the programmatic entry point (§9)
  schema/
    artifact.py             # CapabilityArtifact, StepSpec, locators, predicates, detectors, ValueRef...
    result.py               # ReplayResult, StepResult, FailureDetail, BlockedDetail,
                            # InterventionRequest, InterventionRecord, DiscoveryResult
    app_profile.py          # AppProfile, MaskSpec, TenantOverlay, StepOverride
    snapshot.py             # BBox, SemanticSnapshot, SnapshotElement, ElementDescriptor, TurnRecord (serialized)
  surface/
    base.py                 # Surface protocol: observe() / act() / read() / screenshot() / resolve();
                            # ConcreteAction, ResolvedElement, ActError
    web.py                  # Playwright implementation (only implementation)
    snapshot.py             # injected-JS enumeration (frame-aware), redacted at build
  policy/
    gate.py                 # ActionGate — the single enforcement choke point; LeaseState enum; ApprovalToken
    redact.py               # Redactor — the only path to disk/stdout for run artifacts
    policy.yaml             # default policy config
  replay/
    engine.py               # deterministic executor (no LLM imports allowed in this package)
    locate.py               # ladder walk + per-tier fingerprint check + resume_point()
    outcomes.py             # detector evaluation + classification
  discovery/
    agent.py                # LLM loop (tools: act / read / finish / give_up / ask_human)
    prompts.py
  distill/
    distiller.py            # TurnRecords -> CapabilityArtifact
  escalation/
    controller.py           # session lease state machine (lock-protected; imports LeaseState from policy.gate)
    operator_app.py         # Flask operator console (daemon thread; default 127.0.0.1:7071)
    recorder.py             # injected JS + exposed binding to capture human actions
  evidence/
    log.py                  # structured JSONL run logger (writes only through the Redactor)
target_app/
  app.py                    # Flask "Member Servicing Console"
  seed.py                   # synthetic member data generator
  templates/ static/
  tenants/alpha.yaml beta.yaml
profiles/
  servicing.yaml            # AppProfile for the target app
specs/
  read_savings_balance.yaml # discovery interface declarations (inputs/outputs) — `discover --spec` (§13)
  open_subaccount.yaml
overlays/
  beta.yaml                 # TenantOverlay for tenant beta
artifacts/                  # saved capabilities land here
runs/                       # per-run evidence (gitignored except curated copies)
evidence/                   # curated evidence (committed)
tests/
  acceptance/               # AW-N tests (§19); drive the real CLI against a real target app
  fixtures/                 # hand-authored artifacts move here after phase 5 (§19 linkage)
docs/decisions.md
docs/progress.md
```

Paths inside artifacts (`app_profile`) and overlays are resolved relative to the repo
root (located from the package), never the current working directory.

## 4. Artifact schema (`anton/schema/`) — the centerpiece

Design notes to preserve in docstrings: the artifact is a **capability contract**, not a
macro. A calling agent must be able to read it and know what state it needs, what to send,
what comes back, and every outcome it can get. A human reviewer must be able to audit
every step, every locator (with its rationale), and every declared outcome. It is fully
decoupled from the model transcript. `CapabilityArtifact.model_json_schema()` is itself
part of the reviewable deliverable, which is why every polymorphic field is a
discriminated union.

Pydantic models (field names final; add validators as listed and as sensible):

```python
SCHEMA_VERSION = "1.0"

class ParamType(str, Enum): STRING; INTEGER; NUMBER; BOOLEAN; ENUM
class ParamSpec(BaseModel):
    name: str; type: ParamType; description: str; required: bool = True
    pattern: str | None = None; enum_values: list[str] | None = None
    sensitive: bool = False          # sensitive values are redacted in all logs/evidence

class OutputType(str, Enum): STRING; INTEGER; NUMBER; BOOLEAN; ENUM   # no secret outputs, ever
class ExtractionSpec(BaseModel):
    locator: LocatorLadder; attr: Literal["text", "value"]
    post_process_regex: str | None = None    # runs first; group 1 if present, else whole match
    normalize: Literal["none", "trim", "money", "digits"] = "trim"   # runs second; "money" strips $ , and whitespace
class OutputSpec(BaseModel):
    name: str; type: OutputType; description: str; sensitive: bool = False
    enum_values: list[str] | None = None
    extraction: ExtractionSpec
    # values are read from the LIVE element (§9), coerced to `type` after normalize;
    # coercion failure is a FAILED result

# --- Locators: a typed union, one class per kind, tier fixed by kind ---
class LocatorTier(IntEnum): SEMANTIC = 1; ANCHORED = 2; VISUAL = 3
class _LocatorBase(BaseModel):
    tier: LocatorTier
    rationale: str                    # REQUIRED: why this locator is expected to be robust
    frame_path: list[str] = []        # e.g. ["iframe[name=detail]"]; [] = top frame
class RoleNameLocator(_LocatorBase):          # tier 1
    kind: Literal["role_name"]; role: str; name: str; exact: bool = False
class LabelRelativeLocator(_LocatorBase):     # tier 2 — snapshot geometry only, never DOM relations
    kind: Literal["label_relative"]
    relation: Literal["right_of_label", "below_label", "row_cell"]
    anchor_text: str                  # label text; for row_cell: text of ANY cell in the row
    anchor_col_header: str | None = None   # row_cell only: restrict the anchor match to this column
    col_header: str | None = None     # row_cell only: the column header of the cell to return
    target_role: str | None = None    # optional filter on the candidate's computed role
class ExactTextLocator(_LocatorBase):         # tier 2
    kind: Literal["exact_text"]; text: str
class BBoxLocator(_LocatorBase):              # tier 3 — normalized 0..1 to the top page viewport
    kind: Literal["bbox"]; x: float; y: float; w: float; h: float
LocatorCandidate = Annotated[
    RoleNameLocator | LabelRelativeLocator | ExactTextLocator | BBoxLocator,
    Field(discriminator="kind")]
# validator: role_name ⇒ tier 1; label_relative / exact_text ⇒ tier 2; bbox ⇒ tier 3.

class ElementFingerprint(BaseModel):  # sanity check before acting on a resolved element
    role: str | None; name_snippet: str | None
    control_type: str                 # web: HTML tag; desktop: AX control type
    text_snippet: str | None
    # Checked per tier: tiers 1–2 compare role + control_type + name_snippet (+ text_snippet when set);
    # tier 3 compares role + control_type only (so it genuinely survives label drift and sets drift_flag).
    # None is a wildcard. name_snippet and text_snippet are NEVER set for extraction targets
    # or mask ladders (a balance cell's "name" is its value; an SSN cell's name is the SSN).

class LocatorLadder(BaseModel):
    candidates: list[LocatorCandidate]   # ordered; tiers non-decreasing (validator)
    fingerprint: ElementFingerprint

# --- Predicates / signatures: a typed union on kind ---
class UrlMatches(BaseModel):     kind: Literal["url_matches"]; regex: str
    # compares the URL *path* after stripping the active route_prefix (AppProfile, overridden by TenantOverlay)
class TitleMatches(BaseModel):   kind: Literal["title_matches"]; regex: str
class TextVisible(BaseModel):    kind: Literal["text_visible"]; regex: str     # over visible static text (§7)
class ElementVisible(BaseModel): kind: Literal["element_visible"]; ladder: LocatorLadder
class FieldValue(BaseModel):     kind: Literal["field_value"]; ladder: LocatorLadder; expected: ValueRef
    # with a SecretRef `expected` asserts only that the field is non-empty (the secret is never read back);
    # with a ParamRef/LiteralValue asserts equality against the live value (§9)
class Checked(BaseModel):        kind: Literal["checked"]; ladder: LocatorLadder; expected: bool = True
class AllOf(BaseModel):          kind: Literal["all_of"]; predicates: list[Predicate]   # conjunction, for detector `when`
Predicate = Annotated[UrlMatches | TitleMatches | TextVisible | ElementVisible | FieldValue | Checked | AllOf,
                      Field(discriminator="kind")]
class StateSignature(BaseModel):      # checkpoints are signatures, not "hope the click worked"
    all_of: list[Predicate]

class ActionType(str, Enum): NAVIGATE; CLICK; FILL; SELECT; PRESS; READ
    # READ is a discovery-only tool action (it passes the gate); artifacts never contain READ steps (§10.3)
class RiskClass(str, Enum): SAFE; RISKY

# --- Values: a typed union ---
class LiteralValue(BaseModel): kind: Literal["literal"]; value: str
class ParamRef(BaseModel):     kind: Literal["param"]; name: str
class SecretRef(BaseModel):    kind: Literal["secret"]; name: str   # resolved from env ANTON_SECRET_<NAME> at act-time; value NEVER stored
ValueRef = Annotated[LiteralValue | ParamRef | SecretRef, Field(discriminator="kind")]
# Template tokens: any *string* field inside a LocatorCandidate (name, anchor_text,
# anchor_col_header, col_header, text), ElementFingerprint (name_snippet, text_snippet) or a
# regex predicate may contain `$param:<name>`; the engine expands tokens with the caller's
# ORIGINAL param string (validated, not re-stringified) before resolution/evaluation. This
# is what makes a distilled artifact a capability rather than a macro for the member id it
# was recorded on (§10.3).

# --- Recoveries and detectors: typed unions; payload presence enforced by type ---
class Dismiss(BaseModel):   kind: Literal["dismiss"]; target: LocatorLadder; max_attempts: int = 2
class RetryStep(BaseModel): kind: Literal["retry_step"]; max_attempts: int = 2
class RunFlow(BaseModel):   kind: Literal["run_flow"]; flow_id: str; max_attempts: int = 2   # flow defined in AppProfile.flows
class Escalate(BaseModel):  kind: Literal["escalate"]
RecoveryAction = Annotated[Dismiss | RetryStep | RunFlow | Escalate, Field(discriminator="kind")]
# max_attempts = times this recovery may execute for ONE step within ONE run
class BusinessDetector(BaseModel):
    arm: Literal["BUSINESS_OUTCOME"]; id: str; when: list[Predicate]   # any_of semantics; wrap in AllOf for a conjunction
    outcome_code: str                        # e.g. "MEMBER_NOT_FOUND"
    detail_locator: LocatorLadder | None = None   # element whose LIVE text becomes ReplayResult.outcome_detail (then redacted)
class RecoverableDetector(BaseModel):
    arm: Literal["RECOVERABLE"]; id: str; when: list[Predicate]; recovery: RecoveryAction   # when: any_of
class HardFailureDetector(BaseModel):
    arm: Literal["HARD_FAILURE"]; id: str; when: list[Predicate]; reason: str              # when: any_of
OutcomeDetector = Annotated[BusinessDetector | RecoverableDetector | HardFailureDetector,
                            Field(discriminator="arm")]

class StepSpec(BaseModel):
    id: str                                  # generated by the §10.3 id rule; hand-authored artifacts follow it too
    description: str
    action: ActionType
    target: LocatorLadder | None             # None for NAVIGATE / PRESS-global
    value: ValueRef | None
    risk: RiskClass = RiskClass.SAFE         # effective risk at replay = max(policy match, this): artifacts may RAISE, never lower
    requires_approval: bool = False          # derived: validator forces == (risk == RISKY)
    postcondition: StateSignature            # per-step checkpoint — every step has one
    wait_timeout_ms: int = 8000
    retries: int = 1                         # extra resolve→act→wait cycles when the postcondition times out with NO detector firing
    detectors: list[OutcomeDetector] = []    # step-scoped; evaluated every poll tick alongside the postcondition (§9)
    # validators: risk == RISKY ⇒ requires_approval is True, retries == 0, and no detector
    # carries a RetryStep recovery (re-clicking Confirm on an ambiguous state is the
    # duplicate-transaction bug). The engine applies the same retries == 0 rule to EFFECTIVE
    # risk at run time. FILL/SELECT steps must carry a FieldValue/Checked predicate on their target.

class RecordedAgainst(BaseModel):
    app_id: str; tenant_id: str; app_version: str | None; entry_url: str
class Provenance(BaseModel):
    origin: Literal["discovery", "hand_authored"]
    discovery_run_id: str | None; model: str | None; created_at: datetime; surface: str = "web"
    recorded_against: RecordedAgainst
class Approval(BaseModel):
    approved_at: datetime; approved_by: str
    artifact_sha256: str                     # content hash (below)

class CapabilityArtifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    capability_id: str                       # e.g. "servicing.read_savings_balance"
    name: str; description: str; version: str  # semver, starts "1.0.0"
    status: Literal["draft", "approved"] = "draft"
    approval: Approval | None = None         # validator: status == "approved" ⇒ approval present (hash checked by the loader)
    app_profile: str                         # e.g. "profiles/servicing.yaml" (repo-root relative)
    requires_flows: list[str] = ["login"]    # AppProfile flow ids the engine runs, in order, when preconditions do not hold
    preconditions: StateSignature            # state required before step 1 (e.g. authenticated, at the search page)
    inputs: list[ParamSpec]; outputs: list[OutputSpec]
    business_outcomes: dict[str, str]        # code -> human meaning: the CLOSED outcome space (loader rule below)
    steps: list[StepSpec]
    success: StateSignature                  # final checkpoint; outputs extracted only after it holds
    provenance: Provenance
```

**Content hash.** `artifact_sha256` = sha256 of `model_dump_json()` (sorted keys, no
indent) of the artifact with `status="draft"` and `approval=None`. `ReplayResult.artifact_sha256`
is the same content hash of the artifact actually replayed.

**Loader rules** (run when an artifact is loaded together with its profile, policy and
optional overlay — they need all four, so they live in the loader, not in pydantic
validators): (1) **closure** — every `BusinessDetector.outcome_code` reachable at replay,
i.e. step-scoped detectors ∪ `AppProfile.global_detectors` ∪ overlay `extra_detectors`,
must be a key of `business_outcomes`; the distiller inherits app-level codes
automatically; an overlay may only reference codes already declared. (2) **forbidden
extraction** — no `OutputSpec.extraction` may target an element whose label context
matches `policy.forbidden_extract_patterns`. (3) **approval** — an `approved` artifact whose
recomputed hash differs from `approval.artifact_sha256` is refused with a clear message
(`inspect` reports it); a `draft` artifact ignores any stale `approval` block. (4) `schema_version`
major mismatch is refused. (5) profile patterns ⊆ policy patterns (§8). Loader refusals
exit `2` with a one-line message and no run directory (§5).

**Versioning.** `version`: MAJOR when `inputs`/`outputs`/`business_outcomes`/`preconditions`
change (caller-visible contract), MINOR when steps/locators/detectors change, PATCH for
descriptions/rationale. The distiller emits `1.0.0`; re-distilling an existing
`capability_id` bumps MINOR. `anton approve [--by <name>]` (default `$USER`) computes the
content hash, writes the `approval` block and flips `status`; any later edit invalidates it.

`AppProfile` (yaml, parsed into a model): `app_id`, `tenant_id` (e.g. `alpha`;
`ReplayResult.tenant_id` = the overlay's if present, else this), `app_version: str | None`, `entry_url`,
`route_prefix: str = ""`, `allowed_url_patterns` (must be a subset of the policy's; the
effective allowlist prefixes them with the active `route_prefix`), named `flows` (each a
`list[StepSpec]` — must include `login`, which uses `SecretRef` values, contains **no
NAVIGATE step** (it acts on whatever page the app redirected to, so `?next=` is honoured),
and whose final postcondition is **destination-agnostic**: `ElementVisible` of the
authenticated landmark pinned in §6), `global_detectors: list[OutcomeDetector]` — **all**
app-level detectors of every arm live here, with these pinned ids (§19 asserts on them):

| id | arm | when (any_of) | payload |
|---|---|---|---|
| `session_expired` | RECOVERABLE | `all_of[url_matches ^/login, text_visible "session has expired"]` (the plain login redirect at start-up does NOT match) | `RunFlow(login)` |
| `announcement_modal` | RECOVERABLE | `element_visible` "Dismiss" button in the maintenance modal | `Dismiss(target)` |
| `member_not_found` | BUSINESS | `text_visible "No member found"` | `MEMBER_NOT_FOUND`, detail = that message |
| `validation_error` | BUSINESS | `text_visible "Please correct the errors below"` | `VALIDATION_ERROR`, detail = the inline error |
| `permission_denied` | BUSINESS | `text_visible "do not have permission"` | `PERMISSION_DENIED`, detail = that message |
| `app_error` | HARD_FAILURE | `title_matches "500"` | reason "application error page" |

— because the outcome space is declared per app by a human: one happy-path discovery
cannot observe outcomes it never hit — and `sensitive_elements: list[MaskSpec]`:

```python
class MaskSpec(BaseModel):
    id: str; ladder: LocatorLadder; applies_to: str      # url regex (path, prefix-stripped)
    required: bool = True                       # unresolved required mask ⇒ screenshot fails CLOSED (§7, §8)
```
`sensitive_elements` is the single list used for (a) screenshot masking and (b) scrubbing
element `name`/`text` in every `SemanticSnapshot` at build time (§7, §8) — the model, the
logs and the failure page snapshot all see `***` exactly where the screenshot is black.
`profiles/servicing.yaml` pins three masks: the SSN cell and the Date of Birth cell on
`/member/<id>` (pane), and the "Account Number" value cell on `/subaccount/confirmation`.

`TenantOverlay` (yaml, typed):

```python
class StepOverride(BaseModel):                  # the ONLY fields an overlay may touch on a step
    target: LocatorLadder | None = None
    postcondition: StateSignature | None = None
    wait_timeout_ms: int | None = None
    extra_detectors: list[OutcomeDetector] = []   # Business codes must already be keys of business_outcomes
class TenantOverlay(BaseModel):
    overlay_id: str; tenant_id: str; app_id: str; base_capability_id: str
    entry_url: str | None = None; route_prefix: str | None = None   # e.g. "/portal"
    step_overrides: dict[str, StepOverride] = {}                    # keyed by step id
    output_overrides: dict[str, ExtractionSpec] = {}                # keyed by output name
    success: StateSignature | None = None
```
Overlays may only *narrow or retarget*: they cannot add or remove steps, change an
action/value/input/output name or type, lower a risk class, remove a detector, or
introduce an outcome code. The loader enforces this, and records which ladders came from
the overlay so `StepResult.resolution.source` can report `"overlay"`. Entry URL precedence:
`--url` > `overlay.entry_url` > `profile.entry_url`; `--url` may be origin-only, in which
case the path comes from the next layer (this is how the acceptance fixture's free port
composes with the overlay's `/portal` path).

Also ship `anton inspect <artifact.json>`: renders a human-readable table of the contract
(params, outputs, preconditions/required flows, the *effective* outcome space including
inherited app-level codes, steps with effective risk, chosen locators with rationale and
source, approval validity). This is the "reviewable" requirement made concrete.

## 5. Result contract (`anton/schema/result.py`)

Four terminal statuses. RECOVERABLE is internal only and never a terminal status.
`BLOCKED` means "a human must act; the run is not broken" — filing that under FAILED would
be the glossary's outcome/failure conflation one level up.

Reserved pseudo-step ids, legal wherever a `step_id` appears: `__preconditions__`
(precondition establishment), `__success__` (final signature check), `__outputs__`
(extraction). `ReplayResult.steps` holds one `StepResult` per step actually entered plus
the pseudo-steps that ran, in execution order; steps never reached after a terminal
outcome are **not** appended (so `steps[-1]` is always the step that ended the run);
`SKIPPED` marks only declared steps that `resume_point()` jumped over after a recovery
or a human hand-back.

```python
class ReplayStatus(str, Enum): SUCCESS; BUSINESS_OUTCOME; BLOCKED; FAILED
class FailureKind(str, Enum): CHECKPOINT_MISS; LOCATOR_EXHAUSTED; APP_ERROR; POLICY_VIOLATION
class BlockedKind(str, Enum): APPROVAL_REQUIRED; APPROVAL_DENIED; ESCALATION_UNRESOLVED
class StepStatus(str, Enum): PASSED; BUSINESS_OUTCOME; BLOCKED; FAILED; SKIPPED; HUMAN_COMPLETED

class LocatorResolution(BaseModel):
    tier: LocatorTier; candidate_index: int
    source: Literal["artifact", "overlay"]; fingerprint_ok: bool
class StepResult(BaseModel):
    step_id: str                  # a StepSpec id or a reserved pseudo-step id
    status: StepStatus; attempts: int; duration_ms: int
    resolution: LocatorResolution | None     # None for steps without a target
    drift_flag: bool              # True if only tier 3 (visual) resolved — early-warning signal
    recoveries: list[str]         # detector ids that fired and were handled (incl. inside flows run for this step)
    detectors_fired: list[str]    # every detector that matched at this step, handled or terminal
    evidence: list[str]           # relative paths (screenshots, snapshots) captured at this step

class InterventionRequest(BaseModel):        # written to runs/<id>/intervention.json on trigger
    request_id: str; run_id: str; kind: Literal["TAKEOVER", "APPROVAL"]
    capability_id: str | None; goal: str | None; step_id: str; reason: str
    screenshot: str               # masked, relative path
    page_signature: str
    target_fingerprint: ElementFingerprint | None; target_rationale: str | None
    expected: StateSignature | None
    console_url: str; requested_at: datetime; lease_state: str   # the post-transition value (PAUSED)
class InterventionRecord(BaseModel):
    request_id: str; kind: Literal["TAKEOVER", "APPROVAL"]; at_step: str; reason: str
    requested_at: datetime; control_taken_at: datetime | None; handed_back_at: datetime | None
    resolved_at: datetime | None
    resolution: Literal["resumed", "approved", "denied", "abandoned", "timed_out", "resume_failed"]
    human_action_count: int; human_actions_path: str | None
    resumed_at_step: str | None   # "__outputs__" when the furthest satisfied signature was `success`

class FailureDetail(BaseModel):
    kind: FailureKind; step_id: str | None
    expected: StateSignature | None          # None for POLICY_VIOLATION (nothing was being checked)
    observed_summary: str
    evidence: list[str]           # masked screenshot + redacted page snapshot; may be empty for POLICY_VIOLATION
class BlockedDetail(BaseModel):
    kind: BlockedKind; step_id: str
    intervention_request_path: str | None; lease_state: str

class ReplayResult(BaseModel):
    run_id: str; capability_id: str; version: str
    tenant_id: str | None; overlay_id: str | None; artifact_sha256: str
    status: ReplayStatus
    outputs: dict[str, str | int | float | bool] | None   # SUCCESS only; coerced per OutputSpec.type
    outcome_code: str | None; outcome_step_id: str | None     # BUSINESS_OUTCOME only
    outcome_detector_id: str | None; outcome_detail: str | None  # detail is the observed message, redacted
    failure: FailureDetail | None       # FAILED only
    blocked: BlockedDetail | None       # BLOCKED only
    steps: list[StepResult]; interventions: list[InterventionRecord]
    started_at: datetime; finished_at: datetime
    redacted_view: bool                 # True on stdout and in result.json; only --outputs-file carries raw sensitive outputs

class DiscoveryResult(BaseModel):            # written by `anton discover`
    run_id: str; goal: str
    status: Literal["SUCCESS", "GAVE_UP", "BLOCKED", "FAILED"]
    failure_kind: FailureKind | None    # FAILED only (POLICY_VIOLATION ⇒ exit 30)
    blocked: BlockedDetail | None       # BLOCKED only
    artifact_path: str | None; turns: int; reason: str | None
    interventions: list[InterventionRecord]; started_at: datetime; finished_at: datetime
```

**Exit code is a pure function of the result** (unit test asserts the mapping over every
status/kind combination, for both result models): `0` SUCCESS · `10` BUSINESS_OUTCOME ·
`20` FAILED (or GAVE_UP) with `kind != POLICY_VIOLATION` · `30` FAILED with
`kind == POLICY_VIOLATION` · `40` BLOCKED. Two things happen before any result exists:
invalid `--param` values (fail `ParamSpec` validation) and loader refusals (§4) exit `2`
with a one-line message and no run directory; a draft artifact replayed without
`--allow-draft`, or any artifact refused for `DRAFT_RISKY` (§8), produces a FAILED
`POLICY_VIOLATION` result (exit 30) with a run directory but no browser launch.

stdout and `runs/<id>/result.json` are **always the redacted view** (`redacted_view: true`;
sensitive outputs shown as `"***"`). `--outputs-file PATH` writes the unredacted `outputs`
dict (mode `0600`) for the caller; it is never placed under `runs/` or `evidence/`. In
production, outputs are the return value of the capability call over the agent's channel
and never touch evidence — say so in REPORT §6.

## 6. Target app (`target_app/`) — "Member Servicing Console"

Server-rendered Flask + Jinja, deliberately hostile markup: nested `<table>` layout, no
`id`/`data-testid` attributes, generic class names (`c1`, `row`, `cell`), inline styles,
and the member detail pane rendered inside an `<iframe>` (to force frame-aware
perception). Session via signed cookie. **All interstitials and confirmations are DOM
modals; the app never uses native `alert`/`confirm`/`prompt`.** Labels below are pinned
because policy rules, detectors, gate tests and step ids refer to them. Every
authenticated page renders a **"Sign out"** link in its header — the destination-agnostic
authenticated landmark used by `flows.login` and `preconditions`.

Pages / flow (alpha labels; beta relabels in the tenant paragraph):
- Any unauthenticated request to a non-login route → `302` to `/login?next=<original
  path>` (no `reason`). `GET /login[?next=<path>&reason=expired]` → username + password
  (fake creds seeded: `ops_user` / `demo-pass-1`, in README, marked fake); the form carries
  `next` through the `POST`. Bad creds → inline validation error. Successful `POST`
  redirects to `next` (default `/search`). When `reason=expired` the page shows "Your
  session has expired. Please sign in again."
- `GET /search` → heading "Member Search"; field labelled **"Member ID"** (or last name);
  button **"Search"**. Submit → `GET /member/<id>`.
- `GET /member/<id>` → shell page with heading "Member Profile" and an `<iframe name="detail">`
  loading `GET /member/<id>/pane`: profile table (name; **"Date of Birth"** rendered as ISO
  `YYYY-MM-DD` exactly as stored; **"SSN"** shown as full fake value — it exists precisely
  to demo masking + scrubbing), accounts table with a header row (`Type | Account No. |
  Balance` on alpha) and rows incl. exactly one "Savings" row with a balance cell rendered
  like `$1,234.56` (runtime-created sub-accounts render as "Savings (sub)", so the anchor
  stays unique), and a link **"Open Sub-Account"** (`target="_top"`, so the whole page
  navigates).
- Not-found member id → `GET /member/<id>` renders only "No member found for <id>" (HTTP
  200, message in page — like real legacy apps): **no "Member Profile" heading, no detail
  iframe**.
- `GET /member/<id>/subaccount/new` → form (rendered with `novalidate` and without
  `min`/`required` attributes, so validation is server-side and reaches the documented
  re-render): account type (select), nickname (text), initial deposit (number; server
  minimum $25), terms (checkbox) → button **"Continue to review"**
  (`POST`; NOT irreversible, must not be gated) → `GET /member/<id>/subaccount/review`
  (heading "Review new sub-account") → button **"Confirm and open account"** (`POST`;
  irreversible) → `GET /member/<id>/subaccount/confirmation` → heading "Sub-account
  opened", a table row labelled "Account Number" with the generated number.
- Validation: deposit < $25 re-renders the form with banner "Please correct the errors
  below." and inline "Initial deposit must be at least $25." (HTTP 200).
- Permission denied page: "You do not have permission to open sub-accounts." (HTTP 200).
- App error page: title "500 Internal Server Error".

Runtime-created sub-accounts are kept **in memory** (seed data is read-only). With env
`ANTON_TARGET_DEBUG=1` (set by the acceptance fixture only) the app also serves
`GET /__state/member/<id>` → JSON of that member's accounts, so AW-7 can assert that a
blocked run created nothing.

**Injectable runtime conditions** (env `INJECT=` and/or query `?inject=`; `SLOW_MS` env,
default 5000). Each is defined by trigger route and arity so the acceptance workflows are
deterministic by construction:

| name | fires on | arity | detector (§4) → arm | AW |
|---|---|---|---|---|
| `slow` | every `GET /member/<id>` (the shell route only, not the pane) | every, fixed `SLOW_MS` | none — declarative wait absorbs it | AW-3c |
| `expire` | first **authenticated** `GET /member/<id>` — session cleared, redirect to `/login?next=…&reason=expired` | once | `session_expired` → RECOVERABLE `RunFlow(login)` + resume | AW-3a |
| `modal` | first `GET /search` after login — "Scheduled maintenance notice" DOM modal with a **"Dismiss"** button | once | `announcement_modal` → RECOVERABLE `Dismiss` | AW-3b |
| `deny` | every confirm `POST` on `/member/<id>/subaccount/review` → permission-denied page | sticky | `permission_denied` → BUSINESS `PERMISSION_DENIED` | AW-7c |
| `err500` | every `GET /member/<id>/subaccount/new` → 500 page | sticky | `app_error` → HARD_FAILURE | AW-4 |
| (none) | deposit < $25 → validation re-render | — | `validation_error` → BUSINESS `VALIDATION_ERROR` | AW-7d |

Tenant variants (`tenants/alpha.yaml`, `tenants/beta.yaml`, selected by `TENANT=` env):
beta changes visible labels ("Member ID" → "Customer Number", "Open Sub-Account" → "Add
Product", "Confirm and open account" → **"Complete"**), shifts column order in the accounts
table (`Account No. | Balance | Type` — the row anchor "Savings" moves to the last column),
and prefixes every route with `/portal` (unprefixed routes return a plain `404`, never a
redirect). Same underlying app — this is the stand-in for two institutions running one
vendor product. `overlays/beta.yaml` makes the alpha-recorded
artifacts pass on beta; the URL-of-commit risk rule (§8) must still gate "Complete".

`seed.py` generates ~25 synthetic members to `target_app/data.json` (fixed RNG seed;
clearly fake names, SSNs of form 900-XX-XXXX, DOBs as ISO strings, balances as JSON
numbers). It **always** emits the canonical demo member `204817` and the second member
`310552` (used by AW-1b) regardless of the RNG.

## 7. Surface adapter (`anton/surface/`)

```python
class BBox(BaseModel): x: float; y: float; w: float; h: float   # TOP-PAGE CSS pixels (frame offsets applied)
class SnapshotElement(BaseModel):
    ref: str; role: str; name: str; text: str; control_type: str
    frame_path: list[str]                    # segments are `iframe[name=<name>]`, or `iframe[<index>]` when unnamed
    bbox: BBox
    enabled: bool; visible: bool; value: str | None; checked: bool | None
class SemanticSnapshot(BaseModel):           # redacted at build; this is the failure "page snapshot" format
    url: str; title: str; frame_count: int; elements: list[SnapshotElement]
class ResolvedElement(BaseModel):            # never serialized
    ref: str; frame_path: list[str]; bbox: BBox; control_type: str
    role: str; name: str                     # LIVE accessible role/name (unredacted) — for fingerprints and gate name_matches
    handle: Any                              # live handle

class ActError(Exception):            # never a raw Playwright exception
    kind: Literal["obscured", "detached", "navigation", "timeout"]; detail: str

class Surface(Protocol):
    def observe(self) -> SemanticSnapshot: ...                  # redacted at build (§8)
    def act(self, action: ConcreteAction, timeout_ms: int) -> None: ...   # after ActionGate approval; raises ActError
    def read(self, el: ResolvedElement, attr: Literal["text", "value"]) -> str: ...   # LIVE, unredacted (§9)
    def screenshot(self, masks: list[MaskSpec]) -> Path: ...    # fails CLOSED (below)
    def resolve(self, candidate: LocatorCandidate) -> ResolvedElement | None: ...   # ONE candidate
```

`ConcreteAction` = `(ActionType, target: ResolvedElement | None, value: str | None,
url: str | None)` — the fully resolved action the gate sees and the surface executes.

`SemanticSnapshot` enumerates, **across all frames**: interactive elements, landmarks,
headings, table cells, and every visible block-level static text container (role
`text`) — so banners, inline validation messages and "No member found" are matchable by
`TextVisible`, `ExactTextLocator` and `detail_locator`. `ref` is turn-scoped (e1, e2…);
role is computed from tag/type/ARIA; name from label/aria-label/nearest text. `visible`
is recursive: an element is visible iff it intersects the viewport, `elementFromPoint`
at its centre in its own document is the element or a descendant, AND (for framed
content) the parent document's `elementFromPoint` at the centre mapped to parent
coordinates is the containing iframe — so an element behind a top-frame modal is *not*
visible even inside the pane, which is what makes interstitial detection honest. Build it
by injected JS traversal per frame — do NOT depend on clean DOM, ids, or test attributes
anywhere. The builder applies the `Redactor` (§8) to element `name`/`text` before
returning, so this enumeration — the only thing the LLM sees during discovery (as a
≤ ~60-element digest), the only thing tier-1/2 resolution runs against during replay, and
the only thing ever logged — is redacted at the source. Perception is identical on both
paths. `observe()` during a pending navigation returns an empty snapshot (no signature
holds for that tick) rather than raising.

**Ladder walk** (`replay/locate.py`, surface-independent): expand `$param:` tokens, then
for each candidate in order call `Surface.resolve(candidate)` and check the result against
`ElementFingerprint` with the per-tier strictness in §4; mismatch ⇒ treat as unresolved
and fall to the next candidate. Return which tier and candidate resolved (feeds
`StepResult.resolution` and `drift_flag`). `Surface.resolve` implements one candidate:
tier 1 role+name (frame-aware); tier 2 relations computed **over snapshot geometry
only**, comparing only elements that share the candidate's `frame_path` —
`right_of_label` (nearest element with a matching role to the right of the label's bbox on
the same row band), `below_label`, `row_cell` (the cell at the intersection of the row
that contains *any* cell — or, when `anchor_col_header` is set, the cell under that
header — whose text matches `anchor_text` (first matching row in document order), and
the column whose header matches `col_header`; this is what survives beta's column
shift) — never DOM parent/sibling
relations, so a desktop adapter can implement them from any element list with bboxes;
tier 3 normalized bbox → element at point.

`act()` sets the Playwright action timeout to the step's `wait_timeout_ms` (never the
30 s default) and does not wait for a resulting navigation to finish; a Playwright
`TimeoutError` becomes `ActError("obscured"|"timeout")`, which the engine treats as a
postcondition miss at t=0 (§9). The implementation registers a dialog handler that logs
any native dialog (type + scrubbed message) to run.jsonl and dismisses it; the target app
never raises one (§6), and REPORT §6 lists "native dialogs during a RISKY step" as a
limit.

`screenshot()` resolves each `MaskSpec` whose `applies_to` matches the current path
first; masks are applied by injecting an opaque overlay into the element's own frame
before capture (so masks work inside the iframe); if a `required` mask fails to resolve,
the image is replaced by a generated solid-colour PNG and `mask_unresolved{mask_id}` is
logged — masks fail closed, never open.

Everything upstream (replay, discovery, distiller, escalation) talks only to `Surface`.
The Playwright class is the sole implementation, but the seam is the multi-surface
answer: a legacy web or desktop adapter reimplements observe/act/read/resolve/screenshot
while artifacts, the ladder walk and the replay engine stay untouched. Say this in module
docstrings.

## 8. Policy engine (`anton/policy/`)

`policy.yaml` (loaded from `--policy <path>`, default `anton/policy/policy.yaml`; string
values support `${ENV}` / `${ENV:-default}` expansion):

```yaml
allowed_origins: ["${ANTON_TARGET_ORIGIN:-http://localhost:8000}"]
# glob semantics: "*" matches exactly one path segment, "**" any depth (fnmatch on segments)
allowed_url_patterns: ["/login", "/search", "/member/**", "/portal/login", "/portal/search", "/portal/member/**"]
allowed_actions: [NAVIGATE, CLICK, FILL, SELECT, PRESS, READ]
risky_rules:            # matched => RiskClass.RISKY (effective risk = max(policy, step.risk))
  - actions: [CLICK, PRESS]
    url_matches: ".*/subaccount/review$"        # any commit on the review page is irreversible (label-independent)
  - actions: [CLICK]
    name_matches: "(?i)^(confirm|complete|open account).*"
    url_matches: ".*/subaccount.*"
redact_field_patterns: ["(?i)ssn", "(?i)social security", "(?i)pass", "(?i)token", "(?i)secret", "(?i)dob", "(?i)date of birth", "(?i)account n(o\\b|umber)"]
redact_value_patterns: ["\\b\\d{3}-\\d{2}-\\d{4}\\b"]
forbidden_extract_patterns: ["(?i)ssn", "(?i)social security"]   # no artifact may declare an output extracted from these
```

**Secrets.** `SecretRef(name)` resolves to env `ANTON_SECRET_<NAME>` (upper-cased) at
act-time. `.env.example` ships `ANTON_SECRET_DEMO_PASSWORD=demo-pass-1` marked fake; the
acceptance fixture exports it. `verify-evidence` does not scan `.env*`.

**Effective allowlist** = policy (ceiling: origins, url patterns, action types) ∩
`AppProfile.allowed_url_patterns` prefixed with the active `route_prefix` (app
narrowing). The loader rejects a profile whose patterns are not a subset of the policy's.
Enforcement has two layers: (1) the pre-action gate; (2) a post-navigation check — after
any action that changed the URL, a URL outside the effective allowlist is
`Deny(POST_NAV_OFFLIST)`: the run stops and the page is never observed or screenshotted
(so `FailureDetail.evidence` is empty in that case).

`ActionGate.check(action, context) -> Allow | Deny(reason) | NeedsApproval(reason)` is
the **single choke point**: discovery and replay both pass every action through it before
`Surface.act`. `context` carries the current URL, lease state, invocation consent flags,
artifact status, and the resolved element descriptor. Deny reasons: `ORIGIN`,
`URL_PATTERN`, `ACTION_TYPE`, `SECRET_LITERAL` (a FILL whose literal equals a resolved
secret value), `POST_NAV_OFFLIST`, `DRAFT_NOT_ALLOWED`, `DRAFT_RISKY`, `LEASE_NOT_HELD`.
All but the last are policy violations (FAILED, `POLICY_VIOLATION`, exit 30).
`LEASE_NOT_HELD` is not a policy violation: it is the guard that makes "who is in control"
enforceable — the engine never issues agent actions unless the lease is `AGENT_ACTIVE`,
and a refusal is logged as an internal error, never executed. `LeaseState` and
`ApprovalToken` are defined in `policy/gate.py` (phase 3) and imported by
`escalation/controller.py`. Every gate decision is logged to run.jsonl with `lease_state`
and `owner`.

**Risk and approval.** Effective risk = `max(policy risky_rules match on the concrete
action at act-time, step.risk)`; policy is a floor the artifact and overlays can only
raise; the engine's retry policy uses *effective* risk (§9). `NeedsApproval` is returned
for every RISKY action. Consent:
- `--approve-risky` on `replay` or `discover` = explicit caller consent for the whole
  invocation, logged as gate decision `approval_used{by: "caller"}`.
- Without it: **replay** → the run stops *before* the action with `BLOCKED(APPROVAL_REQUIRED)`
  (exit 40) — or, when `--escalate` is set, raises an `APPROVAL` intervention (§11) and
  waits; **discovery** → same rule (an APPROVAL intervention under `--escalate`, else
  `BLOCKED`).
- An operator's one-shot `ApprovalToken(action_id)` (§11) is consumed by exactly that
  action; a second identical action is `NeedsApproval` again.
- **Draft artifacts.** Replaying a draft requires `--allow-draft` (else
  `Deny(DRAFT_NOT_ALLOWED)`, exit 30, no browser). A draft that *declares* any RISKY step
  is refused at start-up — after the §4 loader rules, before the browser launches —
  regardless of `--allow-draft` (`Deny(DRAFT_RISKY)`, `step_id` = the risky step, exit 30); a draft whose step becomes RISKY only by *effective*
  risk at act-time is denied at that action with the same reason. Irreversible actions
  never run from an unreviewed, LLM-distilled artifact.

**Redaction (`redact.py`).** A single `Redactor`, owned by the run, is the only path to
disk/stdout for run artifacts. `scrub(obj)` replaces, in every string of every object it
is given: (1) any value in the run's known-sensitive set (resolved `SecretRef` values,
values of `sensitive` params, and — as soon as they are read — values of `sensitive`
outputs); (2) any match of `redact_value_patterns`; (3) the `name`/`text` of any
**value-bearing** snapshot element whose bbox lies inside a `sensitive_elements` mask
ladder for the current page, or whose nearest-label context matches
`redact_field_patterns` — an element whose *own* text is the matching label or column
header (e.g. the "SSN" cell, the "Account No." header) is the anchor and is never
scrubbed, and for interactive form controls only `value`/`text` is scrubbed, never the
accessible `name` (the "Password" input keeps its name so the login ladder resolves; the
typed value is what is sensitive). Rule (3) is applied **at snapshot build time**, so it reaches the model-bound
digest, run.jsonl observation events, and the failure page snapshot alike. Live reads
(§9 extraction, `FieldValue`, `detail_locator`) bypass the snapshot by design and register
sensitive values in the known-sensitive set immediately. Callers: the run logger, the
page-snapshot writer (the failure "page snapshot" is the redacted `SemanticSnapshot`
JSON — never raw HTML), the transcript writer (which records the exact request/response
payloads, already scrubbed — there is no post-hoc sanitization step), the human-action
recorder, `InterventionRequest`, and CLI stdout. Secrets are resolved from env at act-time
and never appear anywhere. Screenshots are always taken with `sensitive_elements` masks
applied and fail closed (§7).
Unit tests (§16): a fake SSN in a *nameless* table cell adjacent to an "SSN" label is
scrubbed while the "SSN" label cell itself is not, and the SSN mask ladder still resolves
on the redacted snapshot; a raw SSN in free text is scrubbed by pattern; the SDK request
payload built from a snapshot containing the seeded SSN does not contain it (fake client);
no file under `runs/` contains any seeded SSN after a replay.

## 9. Replay engine (`anton/replay/`) — no LLM imports allowed in this package

**Public entry point:** `anton.api.invoke(artifact, params, *, overlay=None, policy=None,
url=None, approve_risky=False, allow_draft=False, escalate=False, escalate_timeout_s=600,
console_port=7071, outputs_file=None, record_video=False, run_dir=None) -> ReplayResult`.
The CLI is a thin wrapper; README shows the three-line "call it from Python" form.

**Start-up.** Validate params against `ParamSpec` (exit 2 on failure). Load artifact
(+ overlay, + policy, + profile; all §4 loader rules run here, then the draft checks of
§8). Launch the surface at the effective entry URL (§4 precedence). **Establish
preconditions** as a pseudo-step `__preconditions__` (it gets a `StepResult`): poll the
`preconditions` signature for **one tick** (it cannot hold on the login redirect); if it
does not hold, run each flow in `requires_flows` in order, then re-poll with the first
step's `wait_timeout_ms`. Global RECOVERABLE detectors are evaluated on every tick of
this phase, including the first. Detector hits during this phase —
in the poll or inside a flow step — are recorded on `__preconditions__`
(`recoveries`/`detectors_fired`); a RECOVERABLE hit executes its recovery and the loop
simply re-polls (no `resume_point`); a BUSINESS/HARD_FAILURE hit terminates with
`step_id="__preconditions__"`. Flow-step results are logged in run.jsonl under the
pseudo-step, not appended to `ReplayResult.steps`. Any flow step that fails (ladder
unresolved or postcondition timeout) or the signature still failing after the flows ⇒
FAILED `CHECKPOINT_MISS` at `__preconditions__` with `observed_summary` naming the flow
step.

**Per step:** expand `$param:` tokens → evaluate the AppProfile's RECOVERABLE global
detectors once *pre-act* (a known interstitial is dismissed proactively; counts as one
recovery and one `max_attempts` use, no `resume_point`) → resolve ladder — **polled on the
same 250 ms tick loop until `wait_timeout_ms`** (the pane iframe may land a tick after the
shell; `LOCATOR_EXHAUSTED` only when nothing resolves by the deadline, and the wait
consumes the step's budget) → gate → act →
**poll every 250 ms until `wait_timeout_ms`; each tick evaluates, in order, (a) the
postcondition, (b) step-scoped detectors, (c) AppProfile global detectors; the first to
hold wins.** An `ActError` is a tick at t=0 with the postcondition false. Log an
`observation` event (§12) at least once per step pre-act and on every URL change; the
pre-act observation is **the snapshot on which the ladder resolved** — the last
observation logged before `act` (AW-4's `frame_path` assertion depends on it).
- Postcondition holds → next step.
- **RECOVERABLE** detector → execute the recovery (`Dismiss` / `RetryStep` / `RunFlow`),
  bounded by `max_attempts` per step per run, record in `StepResult.recoveries`, then
  **re-localize**: `locate.resume_point(snapshot, steps, start=last_confirmed)` evaluates the
  postconditions of every step after the last confirmed one plus `success`, picks the
  **furthest** signature that currently holds, and continues after it (if `success` holds,
  go straight to extraction). Nothing holds ⇒ the recovery attempt failed (counts toward
  `max_attempts`); exhausted ⇒ treated as "nothing matched". This is the same function
  human hand-back uses (§11), so there is one resume path.
  **Inside a running recovery** (a `RunFlow`'s steps or a `Dismiss`): flow steps evaluate
  their own postconditions and the global detectors *except* the detector whose recovery
  is currently executing (no re-entrancy — the login page still shows "session has
  expired" while the flow types); any detector that fires inside the flow is recorded on
  the invoking step; a flow-step postcondition miss or unresolved ladder is a failed
  recovery attempt, never a run-level `CHECKPOINT_MISS`.
- **BUSINESS_OUTCOME** detector → stop cleanly at once (no retry): status
  `BUSINESS_OUTCOME`, `outcome_code`, `outcome_step_id`, `outcome_detector_id`,
  `outcome_detail` (live text of `detail_locator`, then scrubbed). Not an error. Exit 10.
- **HARD_FAILURE** detector → capture masked screenshot + redacted page snapshot at once
  (no retry); FAILED `APP_ERROR` with `FailureDetail(step, expected=postcondition,
  observed, evidence)`. Exit 20.
- Nothing held by `wait_timeout_ms` → retry per **retry policy**: a step is re-attempted
  only if `retries` remain *and its effective risk is SAFE*; effective-RISKY steps never
  retry and instead go to FAILED `CHECKPOINT_MISS` with `observed_summary` "ambiguous state
  after irreversible action" (or escalate, below). Retries exhausted → FAILED
  `CHECKPOINT_MISS`; ladder never resolved → FAILED `LOCATOR_EXHAUSTED`; both with
  screenshot + page snapshot.
- Gate `NeedsApproval` without consent → `BLOCKED(APPROVAL_REQUIRED)` at that step
  before any action (§8). Gate `Deny` with a policy-violation reason (every reason in §8
  except `LEASE_NOT_HELD`) → FAILED `POLICY_VIOLATION`; `LEASE_NOT_HELD` is an internal
  error that is logged and never executed, exactly as §8 says.

**Escalation mode** (§11 for the mechanism): replay runs in one of two modes. Default: every
FAILED-class terminal above (ladder exhausted, HARD_FAILURE detector, nothing matched after
retries, RISKY step ambiguous), plus `NeedsApproval` without consent and a detector whose
recovery is `Escalate`, terminates immediately as described (an `Escalate` recovery is
logged and treated as HARD_FAILURE). With `--escalate`: the same conditions instead raise
an `InterventionRequest` (`TAKEOVER`, or `APPROVAL` for the consent case) and park; the
run resumes per §11 or ends `BLOCKED(ESCALATION_UNRESOLVED)` when the operator aborts or
`--escalate-timeout` (default 600 s) elapses, and `BLOCKED(APPROVAL_DENIED)` when the
operator denies. `--escalate` is never implied.

**After the last step:** poll `success` as pseudo-step `__success__` with the last step's
`wait_timeout_ms` and the same tick loop (signature, then global detectors): a
BUSINESS/HARD_FAILURE hit reports `step_id="__success__"`; timeout ⇒ FAILED
`CHECKPOINT_MISS` at `__success__` with `expected=success`. Then run output extractions as
pseudo-step `__outputs__`: resolve each ladder, **read the value from the live element via
`Surface.read`** (never from the redacted snapshot), register it in the Redactor's
known-sensitive set when the output is `sensitive`, then `post_process_regex` →
`normalize` → coerce to `OutputSpec.type`; a coercion or extraction failure is FAILED
`CHECKPOINT_MISS` at `__outputs__` with the observed string in `observed_summary`
(scrubbed). `FieldValue` predicates likewise compare against the live value. Return
SUCCESS.

Determinism means: fixed step order, declarative waits (no sleeps other than the tick
poll), bounded retries, no branching beyond declared detectors, one resume path, and zero
model calls — enforce with a test that imports `anton.replay` and asserts `anthropic` is
not in its module graph.

## 10. Discovery agent (`anton/discovery/`) + distiller (`anton/distill/`)

### 10.1 Loop contract
Before the first model turn the harness runs `AppProfile.flows.login` **through the
replay engine** (no model) and tells the model it is authenticated at the entry URL. The
model never sees credentials. The capability's interface is **declared, not inferred**:
`discover --spec <yaml>` (§13) carries `inputs: list[ParamSpec]` and the outputs' `name`,
`type`, `description`, `sensitive`, `enum_values` (everything in `OutputSpec` except
`extraction`); `--param` values are validated against it (exit 2). Per turn the model
receives: the redacted snapshot digest (≤ ~60 elements: `ref/role/name/text/frame`), the
masked screenshot, the goal, the declared params (names, descriptions, and *values* of
non-sensitive params), the declared output names/descriptions, and a short history. Refs
(`e1, e2…`) are turn-scoped; what is persisted is the element's descriptor, never the
ref. Tools (tool use):
- `act(ref | null, action, value)` — `ref` is null only for NAVIGATE (`value` = URL) and a
  global PRESS (`value` = key name); `value` may be a literal, `"$param:<name>"`, or
  `"$secret:<name>"` (resolved by the Surface at act-time; the gate denies a FILL whose
  literal equals a resolved secret). Every `act` goes through the ActionGate; a RISKY
  action without `--approve-risky` follows §8 (APPROVAL intervention under `--escalate`,
  else `BLOCKED`). An operator Deny returns the tool result "denied by operator" and the
  model continues (typically to `give_up`).
- `read(ref)` — returns the element's redacted text.
- `finish(outputs: dict[str, ref])` — **outputs cite the ref they were read from**; the
  harness extracts the value deterministically and the distiller builds the
  `ExtractionSpec` from that ref's descriptor. The model never transcribes values.
- `give_up(reason)`, `ask_human(reason)` (under `--escalate`: TAKEOVER intervention;
  otherwise mapped to `give_up` — the system prompt tells the model whether a human is
  available).
Stop conditions: `finish`, `max_steps=30`, wall clock 5 min, or **dead-end**: page
signature = canonical URL + landmark text + sha256 of the snapshot's
`(ref, role, name, value, checked, enabled)` tuples; dead-end = 3 consecutive non-READ
turns with identical `(action, target descriptor)` and unchanged post-action signature,
OR 6 consecutive non-READ turns with no signature change. READ turns never advance the
counter. Dead-end → TAKEOVER intervention under `--escalate`, else `give_up`.

### 10.2 Recording contract
`runs/<id>/turns.jsonl`, one `TurnRecord` per turn:

```python
class ElementDescriptor(BaseModel):          # the same shape the human-action recorder emits (§11)
    role: str; name: str; text: str; control_type: str; frame_path: list[str]; bbox: BBox
    label_context: str | None                # nearest row/label text
    row_anchor: str | None; col_header: str | None   # when the element sat in a table with a header row
class TurnRecord(BaseModel):
    turn: int; digest_path: str; screenshot_path: str; rationale: str
    tool: str; args: dict[str, str]; gate_decision: str
    executed: ElementDescriptor | None; action: ActionType | None; value_ref: ValueRef | None
    page_signature_after: str
```
Raw model request/response payloads (already scrubbed) go to `runs/<id>/transcript.jsonl`;
the artifact never references either file. Human actions captured during a discovery
TAKEOVER (§11) are recorded to `human_actions.jsonl` with `origin: "human"` as evidence
only; after hand-back the agent re-observes and continues.

### 10.3 Distillation rules
`TurnRecord`s → `StepSpec`s. (1) **Binding:** a provided param value becomes
`$param:<name>` on a *whole-token* match (word boundaries): for `value` fields and
`FieldValue.expected`, numbers are compared after `normalize` (so a typed `50` binds); for
locator strings (`name`/`anchor_text`/`anchor_col_header`/`col_header`/`text`),
fingerprint snippets and predicate regexes, only exact whole-token string equality binds
(`$50.00` on a page never binds to `50`); URLs are canonicalized *before* binding;
`$secret:` tokens are preserved as `SecretRef`; a literal equal to a secret value is a
distiller error. (2) **Ladders** from the recorded descriptor:
tier 1 `role_name`; tier 2 `row_cell` (with `anchor_col_header` = the header of the
anchor's column) when the element sat in a table with a header row, else
`right_of_label`/`below_label` from the captured label context; tier 3 normalized bbox —
each with a generated `rationale`. For extraction targets the element's own text is never
used as a tier-1 name and the fingerprint carries no snippets (a balance cell's "name" is
its value); prefer `row_cell`. (3) **Postconditions:** the observed next-state signature
with the URL canonicalized to a path regex (`/member/204817` → `^/member/[^/]+$`,
prefix-stripped) plus the
**landmark rule**: the most salient static heading/landmark text present after the action
and absent before, excluding any param value or read value; FILL/SELECT/checkbox steps get
`FieldValue` / `Checked` predicates on their target. (4) **Outputs** from `finish`'s refs
(§10.1). (5) **Detectors:** attach `AppProfile.global_detectors`; inherit their codes into
`business_outcomes` (closure rule, §4). (6) **Preconditions:** `requires_flows = ["login"]`;
`preconditions` = the signature observed after the harness login (URL + "Sign out"
landmark). (7) **READ turns are never steps** — they only inform `ExtractionSpec`s; all
are pruned. (8) `risk` per step is copied from the gate decision recorded for that action.
(9) `provenance.recorded_against` from the profile (`app_id`, `tenant_id`, `app_version`)
and the entry URL in use. (10) **Step ids** are deterministic: `<action lower>_<snake_case
slug of the target's accessible name, first four words>` (`click_search`,
`fill_member_id`, `click_open_sub_account`, `click_continue_to_review`,
`click_confirm_and_open_account`); NAVIGATE → `navigate_<path slug>`; global PRESS →
`press_<key slug>`; a collision appends `_2`, `_3`…; hand-authored artifacts use the same
rule. (11) **Interface:** `inputs` and every `OutputSpec` field except `extraction` are
copied verbatim from the `--spec` declaration (§10.1); the distiller synthesises only
`ExtractionSpec` (`normalize: money` when the read text matches a currency pattern, else
per type). (12) **`success`** = the last step's postcondition ∪ `ElementVisible` of every
output's extraction ladder (which forces the pane to be loaded before `__outputs__`).
Output is `status: draft`; a run that contained a human TAKEOVER is always draft. Replaying a draft requires `--allow-draft` (approval gate: the author reviews with
`anton inspect`, flips to approved via `anton approve <artifact>`).

### 10.4 What discovery cannot learn
A single happy-path run cannot observe business outcomes it never hit, recoverable
conditions that did not occur, or alternative branches — which is why the outcome space
and recoveries are declared per app in the `AppProfile` by a human, and why human actions
captured during discovery are evidence rather than steps (distilling them is a next step,
REPORT §7). Say this in REPORT §2/§3.

## 11. Escalation (`anton/escalation/`)

**Session lease** state machine (`LeaseState` in `policy/gate.py`; `Lease` object in
`controller.py` with `request(transition) -> LeaseState`, lock-protected). Edges:
`AGENT_ACTIVE → PAUSED` (trigger) · `PAUSED → HUMAN_ACTIVE` (take) · `HUMAN_ACTIVE →
RESUMING` (hand back) · `RESUMING → AGENT_ACTIVE` (resume succeeded) · `RESUMING → PAUSED`
(re-escalate once) · `PAUSED → AGENT_ACTIVE` (approve, or deny) · `PAUSED → ABANDONED` and
`HUMAN_ACTIVE → ABANDONED` (abort or timeout). Single `owner` field. `ActionGate` refuses
agent actions unless the lease is `AGENT_ACTIVE` (`LEASE_NOT_HELD`, §8) — this makes "who
is in control" enforceable, not advisory. Every transition is logged to run.jsonl.
`/api/take`, `/api/approve`, `/api/deny` are valid only from `PAUSED`; `/api/handback`
only from `HUMAN_ACTIVE`; `/api/abort` from `PAUSED` or `HUMAN_ACTIVE`; anything else → 409.

**Triggers (when escalation is enabled with `--escalate`):** discovery dead-end /
`ask_human`; replay ladder exhausted, HARD_FAILURE detector, checkpoint still failing after
retries, an `Escalate` recovery; and `NeedsApproval` without consent (discovery or replay).
On trigger: transition to `PAUSED`, then write `InterventionRequest` (§5; `kind` =
`APPROVAL` for the consent case, else `TAKEOVER`) to `runs/<id>/intervention.json`, start
the console if not already running, print the console URL on stderr, and wait.

**Operator console** (`operator_app.py`; plain Flask, no styling effort; default
`http://127.0.0.1:7071`, override with `--console-port`; note: macOS AirPlay Receiver
occupies 5000 and 7000). Shows the pending request: goal/capability, step description,
reason, masked screenshot, the target's fingerprint + rationale, the expected signature,
and the lease state. Endpoints — the HTML buttons call these, and AW-5a asserts against
them: `GET /` (HTML), `GET /api/state` → `{lease, owner, pending: InterventionRequest |
null}`, `POST /api/take` (→ `HUMAN_ACTIVE`, recorder armed), `POST /api/handback`
(→ `RESUMING`), `POST /api/approve` (APPROVAL only: one-shot `ApprovalToken(action_id,
issued_by="operator", issued_at)`; lease → `AGENT_ACTIVE`; the gate accepts exactly that
action once and logs `approval_used{by: "operator"}`), `POST /api/deny` (APPROVAL only:
lease → `AGENT_ACTIVE`; the pending action is refused; replay terminates
`BLOCKED(APPROVAL_DENIED)` with `resolution: "denied"`; discovery receives the tool result
"denied by operator" and continues), `POST /api/abort` (→ `ABANDONED`; run ends
`BLOCKED(ESCALATION_UNRESOLVED)`, `resolution: "abandoned"`). Both intervention kinds offer
Take control / Hand back / Abort; APPROVAL additionally offers Approve once / Deny. The
human drives the *actual headed Chromium window* the run owns — same session, same
cookies.

**Concurrency model.** The process has exactly one Playwright-owning thread (the CLI
thread that launched the browser). The console runs in a daemon thread
(`app.run(threaded=True, use_reloader=False)`) and MUST NOT reference any Playwright
object; its handlers only call `Lease.request(transition)` on the lock-protected object
and read already-written files. While the lease is `PAUSED`/`HUMAN_ACTIVE` the owning
thread loops on `page.wait_for_timeout(250)` — this is an event-loop pump, not a
determinism sleep, and it is the only sanctioned wait: a `time.sleep` here would starve
the exposed binding and no human action would ever be recorded — applying transitions it
observes (arm/disarm the recorder, `bring_to_front`, log lease events) and enforcing
`--escalate-timeout`. The timeout clock starts at `PAUSED` and is reset on every lease
transition and on every recorded human action; expiry → `ABANDONED` with `resolution:
"timed_out"`.

**Recorder.** `context.add_init_script(recorder.js)` and
`context.expose_binding("__anton_record", cb)` are installed at browser-context creation
(before any navigation) so every document, frame and popup is instrumented; recording is
armed/disarmed by a Python-side flag checked in the callback (JS always calls the binding;
disarmed calls are dropped). On arming, the owning thread also `evaluate`s the recorder
into every frame already loaded (`page.frames`) as a safety net; the script is idempotent
(`window.__anton_recorder_installed` guard) so listeners are never duplicated. It captures
click/change/submit and `keydown Enter` on inputs as `ElementDescriptor`s (§10.2, built by
the same injected JS as `snapshot.py`), the frame path from the callback's
`source.frame`, and values masked by input type as well as name pattern, to
`runs/<id>/human_actions.jsonl` (through the Redactor) with `origin: "human"`.

**Resume = re-localize, don't assume.** On hand-back the engine observes and calls
`resume_point()` (§9): the step whose postcondition the human satisfied is marked
`HUMAN_COMPLETED`; the run continues at the first step after the furthest satisfied
signature (or extracts outputs if `success` holds — `resumed_at_step` is then
`"__outputs__"`). Nothing holds → re-escalate once (`RESUMING → PAUSED`; the first record
gets `resolution: "resume_failed"`), then FAILED `CHECKPOINT_MISS` with clear detail. In
discovery, hand-back simply re-observes and continues the loop. Append an
`InterventionRecord` (resolution `resumed`, `approved`, `denied`, `abandoned`,
`timed_out` or `resume_failed`, with timestamps and `human_action_count`) to the result.

## 12. Evidence (`anton/evidence/`, `runs/`, `/evidence/`)

`runs/<run_id>/run.jsonl` — one JSON event per line, all through the Redactor. Event
types: `observation` (`{step_id, url, title, frame_count, elements: [SnapshotElement…]}` —
the full redacted enumeration, at least once per step pre-act and on every URL change;
the failure page snapshot is the same structure), (discovery) `rationale`, `gate`
(decision, reason, `lease_state`, `owner`), `action`, `postcondition`, `detector`,
`recovery`, `lease`, `mask_unresolved`, `dialog`. Masked screenshots at each step boundary
for discovery; on failure + final state for replay. Also: `result.json` (redacted view),
`intervention.json` and `human_actions.jsonl` when an intervention occurred, `turns.jsonl`
+ `transcript.jsonl` for discovery, and the redacted page snapshot referenced by
`failure.evidence`.

Curated `/evidence/` (committed) after real runs — `evidence/INDEX.md` maps each folder to
the AW or named gate it demonstrates:
`discovery-run/` (turns + run.jsonl + key screenshots + the scrubbed transcript;
discovery gate), `discovery-run-subaccount/` (discovery gate), `artifact/
read_savings_balance.json` and `artifact/open_subaccount.json` (the approved copies;
discovery gate), `replay-success/` (AW-1), `replay-second-member/` (AW-1b),
`replay-not-found/` (AW-2), `replay-session-expiry/` (AW-3a), `replay-modal/` (AW-3b),
`replay-slow/` (AW-3c), `replay-hard-failure/` (AW-4, with the FailureDetail and the
files it references), `replay-escalation-abandoned/` (AW-5a), `escalation-demo/` (AW-5b:
intervention + human_actions + resumed result), `escalation-demo-discovery/` (phase 6
approval intervention during discovery), `replay-blocked-approval/` (AW-7a),
`replay-subaccount-approved/` (AW-7b), `replay-permission-denied/` (AW-7c),
`replay-validation-error/` (AW-7d), `replay-draft-refused/` (AW-7e), and — after the
overlay phase — `replay-tenant-beta/` (AW-6).

## 13. CLI

```
python -m anton target serve [--tenant alpha|beta] [--inject ...]     # or: make target
python -m anton discover --goal "Look up member 204817 and read the current savings balance" \
    --profile profiles/servicing.yaml --spec specs/read_savings_balance.yaml \
    --param member_id=204817 --out artifacts/
python -m anton inspect artifacts/read_savings_balance.json
python -m anton approve artifacts/read_savings_balance.json [--by <name>]
python -m anton replay artifacts/read_savings_balance.json --param member_id=204817
python -m anton replay ... --param member_id=999999          # -> BUSINESS_OUTCOME MEMBER_NOT_FOUND, exit 10
python -m anton replay ... --overlay overlays/beta.yaml      # tenant beta
python -m anton replay artifacts/open_subaccount.json --param member_id=204817 \
    --param initial_deposit=50 --param nickname="Holiday fund"      # -> BLOCKED(APPROVAL_REQUIRED), exit 40
python -m anton replay artifacts/open_subaccount.json ... --approve-risky --outputs-file /tmp/out.json
```

| flag | discover | replay | meaning |
|---|---|---|---|
| `--goal` | ✓ | | natural-language goal |
| `--profile <path>` | ✓ | | AppProfile (replay reads it from the artifact) |
| `--spec <path>` (required) | ✓ | | declared interface: `inputs` (full `ParamSpec`s) + outputs minus `extraction` (§10.1); copied verbatim into the artifact |
| `--url <entry_url>` | ✓ | ✓ | overrides the entry URL (§4 precedence; may be origin-only) — the brief's input shape is "goal + target" |
| `--param k=v` | ✓ | ✓ | typed inputs; validated against `ParamSpec` (exit 2) |
| `--out <dir>` | ✓ | | where the distilled artifact lands |
| `--overlay <path>` | | ✓ | TenantOverlay |
| `--policy <path>` | ✓ | ✓ | policy file (default `anton/policy/policy.yaml`) |
| `--approve-risky` | ✓ | ✓ | caller consent for RISKY actions in this invocation |
| `--allow-draft` | | ✓ | replay a draft artifact (RISKY steps still refused, §8) |
| `--escalate` | ✓ | ✓ | enable human escalation (default off) |
| `--escalate-timeout <s>` | ✓ | ✓ | default 600 |
| `--console-port <n>` | ✓ | ✓ | default 7071 |
| `--outputs-file <path>` | | ✓ | unredacted outputs for the caller (0600) |
| `--record-video` | ✓ | ✓ | optional Playwright video, off by default; never a gate |

`target serve --tenant/--inject` set `TENANT`/`INJECT` for the spawned app; `approve --by`
fills `Approval.approved_by` (default `$USER`); the table lists discover/replay flags only.

A second capability for the risky/approval path: goal "open a new savings sub-account
with a $50 initial deposit for member 204817 and reach the confirmation screen"; params
`member_id`, `initial_deposit` (NUMBER), `nickname`; output `account_number`. In both
`specs/read_savings_balance.yaml` and `specs/open_subaccount.yaml`, `member_id` is
`STRING` with `pattern: "^[0-9]+$"` (AW-1b's exit-2 leg depends on it).
(`sensitive: true` — an account identifier). `savings_balance` on the first capability is
deliberately `sensitive: false` (a synthetic demo value that must be readable in
evidence); one line in `docs/decisions.md` says why.

## 14. README.md (write it, keep it tight)

Setup (venv, `pip install -r requirements.txt`, `playwright install chromium`,
`cp .env.example .env`), the exact demo path above in order (serve → discover → inspect →
approve → replay → error replay → risky replay blocked → risky replay approved; note the
`make discover` / `make replay` / `make replay-notfound` wrappers), a "call it from
Python" snippet (`anton.api.invoke`), a "no API key?" section (everything except
`discover`, plus the committed example artifacts to replay immediately),
fake-credentials note (incl. `ANTON_SECRET_DEMO_PASSWORD`), the console port note,
pointer to `evidence/INDEX.md`. Open the README with one sentence framing anton as a
digital back-office operator: learns a flow once, replays it exactly.

## 15. REPORT.md — skeleton only, author writes the prose

Create with exactly these seven `##` headings and 3–5 bullet prompts each, every bullet
`[AUTHOR: ...]`: 1. Architecture · 2. Artifact schema · 3. Determinism & error handling ·
4. Heterogeneity & multi-tenant · 5. Escalation & handoff · 6. Safety · 7. Cuts.
Seed the bullets with the decisions actually taken (surface seam, typed ladder + per-tier
fingerprint, four-status contract with pure exit codes, closed outcome space, preconditions
via profile flows, one resume path, lease model + two intervention kinds, overlay rules,
what was mocked). Seeds required under **6. Safety** (the brief's heading is "your
guardrail model *and its limits*"): `[AUTHOR: allowlist model — policy ceiling ∩ profile,
where enforced (pre-action + post-navigation), what it does NOT cover (data-level access)]`
· `[AUTHOR: risk model — URL-of-commit + name rule, effective risk = max(policy, step),
caller/operator approval, draft refusal; why rule-based classification is tenant-fragile]`
· `[AUTHOR: redaction — value set + patterns + label context at every sink incl. the
model-bound digest; residual: unknown PII in free text, mask-resolution dependency, images]`
· `[AUTHOR: what the model sees and what goals that makes impossible]` · `[AUTHOR: limits
you would close next — identity-bound approval, least-privilege app account, data-level
policy, native-dialog coverage, network-level origin blocking]`. Seeds required under
**7. Cuts** (first bullets, in this order): `[AUTHOR: to_tool_definition() — emitting the
artifact as an agent tool schema — first thing to build next]` · `[AUTHOR: distilling
human TAKEOVER actions into artifact steps (recorded as evidence today)]` · `[AUTHOR:
StateSignature.none_of and a caller-facing max_duration hint]` · `[AUTHOR: policy
annotation of human actions; a live lease banner in the window]` · `[AUTHOR: per-step
approval tokens and cross-tenant drift analytics from resolution/tenant fields]`. Do not
write finished prose — the author must own and defend every word.

## 16. Tests (focused, not exhaustive)

- ActionGate: allow / deny origin / deny url pattern (glob semantics: `/member/*` vs
  `/member/**`) / deny action type / risky→NeedsApproval / effective risk =
  max(policy, step) (artifact cannot downgrade) / `SECRET_LITERAL` / `POST_NAV_OFFLIST` /
  `DRAFT_NOT_ALLOWED` / `DRAFT_RISKY` (declared and effective) / `LEASE_NOT_HELD` is refused
  and is *not* a policy violation / an agent NAVIGATE or READ on `/__state/...` is
  `Deny(URL_PATTERN)` (debug endpoints are outside the effective allowlist by
  construction — a checked property, not an assumption) / "Continue to review" on `/subaccount/new` is Allowed
  while "Confirm and open account" and `PRESS Enter` on `/subaccount/review` are
  NeedsApproval / an operator `ApprovalToken` is consumed once and a second identical
  action is NeedsApproval again.
- Redaction: the four tests in §8; screenshot-mask invoked (inside the iframe); a
  deliberately broken required mask produces a blanked image + `mask_unresolved`.
- Exit code mapping: pure function over every `(status, kind)` combination for
  `ReplayResult` and `DiscoveryResult`, plus the exit-2 and exit-30 pre-run cases.
- Distiller: whole-token param binding into value AND locator/fingerprint/predicate
  strings (no over-binding of `50` inside `$50.00`), URL canonicalization before binding,
  secret handling, prefix stripping, ladder generation incl. the no-self-text /
  no-snippet rule for extraction targets, `finish` ref → `ExtractionSpec`, landmark rule,
  `FieldValue`/`Checked` postconditions, inherited outcome codes, all READ turns pruned,
  deterministic step ids, human-takeover run ⇒ draft.
- Discovery loop (fake client): dead-end counter — 3 identical non-READ turns with
  unchanged signature, 6 unchanged non-READ turns, READ never advances; `ask_human`
  without `--escalate` maps to `give_up`.
- Locator ladder on hostile fixture HTML (nested tables + iframe): tier-1 hit; tier-1 miss →
  tier-2 `row_cell` survives a column shift *that moves the anchor column too*;
  `right_of_label`; fingerprint mismatch → next candidate; tier-3 resolves with
  role/control_type-only fingerprint and sets `drift_flag`; `$param:` expansion in a
  locator name; the "SSN" label cell is not scrubbed and the SSN mask ladder resolves on
  a redacted snapshot; visibility is false for a pane element under a top-frame modal.
- Outcome classification: each arm; detector short-circuits the wait; recoverable bounded by
  `max_attempts`; `retries` vs `max_attempts` interaction (AW-3a's "exactly one" depends on
  it); the in-progress recovery's detector is suppressed inside its flow (no double
  `session_expired`); effective-RISKY step never retries; `resume_point()` picks the
  furthest satisfied signature; `__success__` timeout and `__outputs__` coercion failure.
- Schema: artifact + result round-trip; discriminated-union round-trip for locators,
  predicates, values, recoveries, detectors; kind↔tier validator; RISKY ⇒ `retries == 0`
  and `requires_approval`; loader: outcome-space closure (missing code rejected; overlay
  may not introduce a code), approval hash refusal, stale approval on a draft ignored,
  overlay loader rejects added steps / lowered risk / removed detectors and accepts
  `route_prefix` + `output_overrides`, `schema_version` major mismatch rejected,
  `forbidden_extract_patterns` rejects an SSN output.
- Surface (integration): dialog handler logs and dismisses a native `confirm`; action
  timeout equals the step budget (an obscured click raises `ActError` within it); the
  login ladder resolves on a redacted snapshot (password input keeps its name); the
  confirmation-page snapshot scrubs the "Account Number" value cell and keeps its label;
  ladder resolution polls through the pane-load race.
- Escalation triggers (fake surface, `--escalate`): ladder exhausted / HARD_FAILURE
  detector / retries exhausted each write an `InterventionRequest(kind=TAKEOVER)`,
  transition the lease to `PAUSED` and issue no further gate calls; `NeedsApproval`
  without consent writes `kind=APPROVAL`.
- Replay purity: no `anthropic` in `anton.replay` import graph.
- Lease: full transition table incl. approve/deny/re-escalate/`ABANDONED`; invalid
  transition → 409 from the console API.
Playwright-dependent tests marked `@pytest.mark.integration`.

## 17. Build order — do phases in order, stop at each gate

Each phase ends with "prove it" commands; run them and show output before moving on.
Budgets are in sessions (CLAUDE.md: one phase per session) and are binding: a phase that
runs a full session past its budget stops at the next runnable state, records the cut in
`docs/decisions.md` (for phase 7: a line that *starts with* `- P7-CUT ·`; prose mentions
of the marker do not count) and REPORT §7, and the next phase begins. **Phase 7 is cut
first**; `verify-all` honours a recorded cut (§19).

1. **Scaffold + schema** (1 session): repo tree, all §4/§5/§7/§10.2 models with
   validators, `inspect`, `approve`, exit-code mapping. Gate: `make verify-0`.
2. **Target app** (1 session): all pages, pinned labels and landmark, injection table,
   seed (canonical ids), tenant configs, debug state endpoint. Gate: `make verify-target`
   (curls each pinned page and each injection, asserting the pinned strings).
3. **Surface + policy** (2 sessions): redacted snapshot over the hostile DOM (incl. iframe
   and recursive visibility), ladder walk with per-tier fingerprints, gate + Redactor +
   `LeaseState`/`ApprovalToken`. Gate: `make verify-0` + `make verify-surface`.
4. **Replay first** (1 session): write `specs/read_savings_balance.yaml` and
   `specs/open_subaccount.yaml` (the declared interfaces), then hand-author
   `artifacts/read_savings_balance.json` AND `artifacts/open_subaccount.json` (approved,
   `inputs`/`outputs` copied from the specs) against the target app; pass AW-1, AW-1b,
   AW-2, AW-3, AW-4, AW-7. (Replay-before-discovery forces the schema to be a real
   contract, not whatever discovery emits.) **Discovery spike (end of phase 4; needs API
   key; skipped with a clear message if absent):** with the engine's login available, run
   a minimal `discovery/agent.py` loop with `act/read/finish` only — no distiller, no
   artifact — on the balance goal; gate = a `turns.jsonl` in `runs/` showing the model
   reached the member pane and cited the balance ref in `finish`. Phase 5 completes the
   module. De-risks phase 5 before it is paid for.
5. **Discovery + distiller** (1 session, needs API key): real run on the balance goal;
   `inspect` → `approve` → the distilled artifact re-passes AW-1, AW-1b, AW-2, AW-3, AW-4
   (unmodified except for the approval block); then the sub-account goal with
   `--approve-risky`; `approve` → re-passes AW-7. Curate evidence; satisfy the discovery
   gate (§19).
6. **Escalation** (2 sessions): lease, console, recorder, resume. Pass AW-5a (automated).
   Then the two human demos: (a) AW-5b — replay `open_subaccount --escalate` without
   consent, human takes control, clicks "Confirm and open account" in the live window,
   hands back, run resumes to SUCCESS; (b) `discover` on the sub-account goal with
   `--escalate` and no `--approve-risky` → APPROVAL intervention → operator clicks Approve
   once → the model's Confirm executes → distilled artifact carries the RISKY step. Curate
   evidence for both.
7. **Tenant beta + overlay** (1 session; first to cut): alpha artifacts + `overlays/beta.yaml`
   pass on beta (AW-6). If cut: keep `TenantOverlay` + loader tests as the schema-level
   proof and describe the demo in REPORT §7.
8. **Polish** (1 session): README, REPORT skeleton, `evidence/INDEX.md`, `docs/decisions.md`,
   ruff, `make verify-all` (includes `verify-evidence`).

If the author stops you early, whatever phase you're in must still leave the repo
runnable and the cut documented.

## 18. Coverage map (self-check before declaring done)

| Brief requirement | Where satisfied (spec text) | Verified by |
|---|---|---|
| 3.1 goal + target as input | §13 `--goal`, `--url` | AW-1 (`--url`); discovery gate (`--goal`) |
| 3.1 observe → decide → act until goal / max steps / timeout / dead-end | §10.1 loop, stop conditions incl. state-hash dead-end | discovery gate; §16 discovery-loop unit test |
| 3.1 real UI, works without a clean DOM | §7 snapshot: roles computed, no ids/test-ids/CSS, frame-aware; tier-2 over geometry only; tier-3 visual | §16 ladder tests on hostile fixture |
| 3.2 ordered steps | §4 `StepSpec`, deterministic ids (§10.3) | AW-1 |
| 3.2 element identification + robustness reasoning | §4 typed `LocatorCandidate` union, required `rationale`, per-tier fingerprint | §16 schema + ladder; `inspect` |
| 3.2 typed input params | §4 `ParamSpec`, validated pre-flight (exit 2) | AW-1b (second id + invalid-param leg) |
| 3.2 typed outputs + shape | §4 `OutputSpec`/`OutputType`, §9 live read + coercion, §5 typed `outputs` | AW-1 (`Decimal` equality), AW-7b |
| 3.2 checkpoint / success condition | §4 per-step `postcondition` incl. `FieldValue`/`Checked`, `preconditions`, `success` (`__success__`) | AW-1..7 |
| 3.2 versioned, reviewable, agent- and human-readable | §4 versioning rules, content-hash approval, closed outcome space, `inspect` | §16 schema/loader tests |
| 3.3 replay without LLM, stable targeting, verify, return outputs | §9, purity test | AW-1, §16 purity |
| 3.3 runtime condition: validation error | §6 deposit < $25, detector `validation_error` (business) | AW-7d |
| 3.3 runtime condition: record not found | §6, detector `member_not_found` | AW-2 |
| 3.3 runtime condition: permission denial | §6 `deny`, detector `permission_denied` | AW-7c |
| 3.3 runtime condition: unexpected dialog | §6 `modal`, detector `announcement_modal` (`Dismiss`), pre-act evaluation, recursive visibility; native dialogs logged+dismissed | AW-3b; §16 dialog-handler test |
| 3.3 runtime condition: session timeout | §6 `expire`, detector `session_expired` (`RunFlow(login)`) + `resume_point()`, no re-entrancy | AW-3a |
| 3.3 runtime condition: slow / failed load | §6 `slow` (declarative wait, action timeout = step budget) and `err500` (`app_error`, HARD_FAILURE) | AW-3c, AW-4 |
| 3.3 business outcome vs recoverable vs hard failure | §5 statuses + `FailureKind`, §4 detector union, §9 tick evaluation | AW-2, AW-3, AW-4, AW-7 |
| 3.3 structured result: step / expected / observed; every terminal state expressible | §5 `FailureDetail`, `BlockedDetail`, `outcome_step_id`, exit code pure function | AW-2, AW-4, AW-7a, §16 exit-code test |
| 3.4 explicit configurable allowlist | §8 policy ceiling ∩ prefixed profile, glob semantics, `--policy`, pre-action + post-navigation enforcement | §16 gate tests |
| 3.4 safe vs risky, handled conservatively | §8 effective risk, URL-of-commit rule, `BLOCKED(APPROVAL_REQUIRED)`, draft refusal (declared + effective), no retries on effective-RISKY | AW-7a/b/e, §16 |
| 3.4 never persist secrets / PII; redact | §8 Redactor at every sink incl. snapshot build (anchors exempt, values scrubbed); `SecretRef` via env; masks fail closed; model never handles credentials (§10.1) | AW-4 grep + positive signal, `verify-evidence`, §16 |
| 3.5 structured log + richer failure signal | §12 `observation` events + masked screenshot + redacted page snapshot | AW-4 |
| 3.6 detect stuck + route with context | §11 triggers, `InterventionRequest` model | AW-5a; §16 escalation unit test (ladder-exhausted → TAKEOVER under `--escalate`) |
| 3.6 same live session takeover, hand back, record human actions | §11 headed window, lease, recorder | AW-5b (human) |
| 3.6 who is in control | §11 lease enforced by gate (`LEASE_NOT_HELD`) | AW-5a lease log, §16 |
| 3.6 risky step needs a person to decide | §11 APPROVAL intervention (Approve once / Deny) | AW-5a deny leg; phase 6 discovery demo (human) |
| 3.7 surface abstraction seam | §7 `Surface` protocol, surface-independent ladder walk, geometry-only tier 2, `control_type` (REPORT §4 prose) | — (design) |
| 3.7 multi-tenant reuse + drift | §4 typed `TenantOverlay`, `RecordedAgainst`, §5 `resolution.source`/`drift_flag`, §6 beta | AW-6; §16 tier-3 drift test |
| brief §4 real discovery run with evidence | §17 phase 4 spike + phase 5, §1 no-fabrication rule, §19 linkage (content hash + run id) | discovery gate |
| brief §6 README / REPORT 7 headings incl. safety limits / evidence incl. error replays | §14, §15, §12 | `verify-evidence`, INDEX.md |
| Stretch (≤2): approval gating; cross-tenant variant | §4/§8 draft→approved + draft refusal; §17 phase 7 | AW-7e, AW-6 |

## 19. Acceptance workflows (executable gates)

These are the end-to-end workflows the finished system must pass. They mirror the brief's
`/evidence/` requirements, so passing them ≈ having the evidence. Implement each as a
pytest in `tests/acceptance/` plus a `make verify-N` target that a human can run
standalone. Acceptance tests drive the real CLI against a really-running target app
(spawn it as a subprocess fixture on a free port; export `ANTON_TARGET_ORIGIN`,
`ANTON_TARGET_DEBUG=1` and `ANTON_SECRET_DEMO_PASSWORD`; pass `--url` origin-only) — no
mocking inside the verified path. Unless stated, runs are **without** `--escalate`.
Committed fixtures and hand-authored artifacts are `status: approved` with a valid hash.
The artifacts under test are selected with `ANTON_ARTIFACT_DIR` (default `artifacts/`) so
phase 5 re-runs the same tests against the distilled artifacts. Step ids are asserted
**structurally** from the artifact under test — `RISKY_STEP` = the id of the unique step
with `risk == RISKY` (`click_confirm_and_open_account` for the hand-authored artifact),
and "the last step result" = `steps[-1]` — so a distilled artifact passes without edits.
Numeric comparisons load `data.json` with `parse_float=Decimal`.

**Phases 1–3 gates:** `make verify-0` = the §16 unit suite (`pytest -m "not integration"`);
`make verify-surface` = the §16 integration suite (`pytest -m integration`, headless
allowed); `make verify-target` = curl every pinned page and every injection, asserting the
pinned strings — including the unauthenticated `302` to `/login?next=…`, the `?next=`
round-trip through the login POST, the validation re-render for `POST deposit=10`, beta's
plain `404` on an unprefixed route, and the pinned "Sign out" / "Date of Birth" / "SSN" /
"Account Number" labels.

**AW-1 · Happy-path replay** (`make verify-1`)
Serve tenant alpha → replay `read_savings_balance` with member 204817 → assert: exit 0,
`status == SUCCESS`, `Decimal(str(outputs["savings_balance"])) == seeded_balance` (read
from `target_app/data.json`, don't hardcode), for every step with a target
`resolution.tier <= 2` and `fingerprint_ok`, no `drift_flag`, `steps[0].step_id ==
"__preconditions__"` with status PASSED, `runs/<id>/` contains run.jsonl + result.json
with `redacted_view: true`.

**AW-1b · A capability, not a macro** (`make verify-1b`)
Same artifact, `--param member_id=310552` → exit 0, SUCCESS, balance equals *that*
member's seeded value, no `drift_flag`. Second leg: `--param member_id=abc` (fails the
`ParamSpec` pattern) → exit 2, no `runs/<id>/` created.

**AW-2 · Business outcome, not a crash** (`make verify-2`)
Same replay with member id 999999 → assert: exit 10, `status == BUSINESS_OUTCOME`,
`outcome_code == "MEMBER_NOT_FOUND"`, `outcome_detector_id == "member_not_found"`,
`outcome_step_id == steps[-1].step_id` and that step's `status == BUSINESS_OUTCOME`
(`click_search` for the hand-authored artifact), `outcome_detail` contains "No member
found", `failure is None`, `blocked is None`, no traceback anywhere in captured output, and
the detection happened within one `wait_timeout_ms` of the action (detectors
short-circuit the wait).

**AW-3 · Recoverable and transient conditions** (`make verify-3`)
(a) Serve with `INJECT=expire` → same replay → exit 0, SUCCESS, and exactly one
`StepResult` has `recoveries == ["session_expired"]` (proving the login flow ran once and
the run resumed rather than silently passing because the injection didn't fire), and
`__preconditions__` has no recoveries. (b) `INJECT=modal` → exit 0, SUCCESS, exactly one
`StepResult` (which may be `__preconditions__`) has `recoveries == ["announcement_modal"]`.
(c) `INJECT=slow` (`SLOW_MS=5000`) → exit 0, SUCCESS, the step that navigated to
`/member/<id>` has `duration_ms >= 5000`, `attempts == 1`, and `recoveries == []` (the
declarative wait absorbed it; no retry).

**AW-4 · Hard failure is debuggable, and redaction holds under failure** (`make verify-4`)
Serve with `INJECT=err500` → replay `open_subaccount` (no consent flag needed: the failure
precedes the risky step) → assert: exit 20, FAILED, `failure.kind == APP_ERROR`,
`failure.step_id == steps[-1].step_id` with that step's status FAILED
(`click_open_sub_account` for the hand-authored artifact), `failure.expected` equals that
step's postcondition, `failure.observed_summary` non-empty, every path in
`failure.evidence` exists, the masked screenshot bytes were written, the redacted page
snapshot parses as `SemanticSnapshot`. Redaction: run.jsonl contains an `observation` event
whose `url` matches `/member/<id>` and in which some element with `frame_path ==
["iframe[name=detail]"]` has `text == "***"` (positive signal), and grepping the whole
`runs/<id>/` tree for **every** seeded SSN in `data.json` finds nothing.

**AW-5a · Escalation parks, is observable, and can be abandoned** (`make verify-5a`, automated)
Replay `open_subaccount --escalate --escalate-timeout 60` without `--approve-risky` →
within 30 s: `runs/<id>/intervention.json` exists with `kind == "APPROVAL"` and `step_id ==
RISKY_STEP`; `GET /api/state` → `lease == "PAUSED"` with the pending request;
`POST /api/take` → `HUMAN_ACTIVE`; `POST /api/abort` → the CLI exits 40, `status ==
BLOCKED`, `blocked.kind == ESCALATION_UNRESOLVED`, `interventions[0].resolution ==
"abandoned"`, the lease log shows `AGENT_ACTIVE→PAUSED→HUMAN_ACTIVE→ABANDONED`, and no
`action` event appears after the `PAUSED` transition. Second leg: `--escalate-timeout 5`
with no operator → exit 40, `resolution == "timed_out"`. Third leg (per A1): `POST
/api/deny` instead of take/abort → exit 40, `blocked.kind == APPROVAL_DENIED`,
`resolution == "denied"`, lease log `AGENT_ACTIVE→PAUSED→AGENT_ACTIVE`.

**AW-5b · Escalation round-trip** (`make verify-5b`, human, definition-of-done)
Replay `open_subaccount --escalate --escalate-timeout 900 --outputs-file <tmp>` without
`--approve-risky` → the run parks (as 5a). The middle is human: Take control, click
"Confirm and open account" in the live window, Hand back. The script then asserts: exit 0,
SUCCESS, the outputs file's `account_number` matches the app's `/__state` record (stdout
shows `"***"`), `interventions[0].kind == "APPROVAL"`, `resolution == "resumed"`,
`human_action_count >= 1`, `resumed_at_step == "__outputs__"` (the confirm step is the last
step, so resume lands in extraction), the `RISKY_STEP` result has `status ==
HUMAN_COMPLETED`, the lease log shows
`AGENT_ACTIVE→PAUSED→HUMAN_ACTIVE→RESUMING→AGENT_ACTIVE` with no `action` events gated
through during `HUMAN_ACTIVE`. Print clear PASS/FAIL. (CI-skippable; this one is run by
the author, by design.)

**AW-6 · Cross-tenant overlay** (`make verify-6`)
Serve tenant beta (`--url` origin-only in both legs) → replay the alpha-recorded
`read_savings_balance` **without** overlay → expect exit 20 FAILED with `failure.kind ==
CHECKPOINT_MISS` at `__preconditions__` (proves beta genuinely differs; a passing run here
means the variant is too weak — fix the variant, not the assertion). Then replay **with**
`overlays/beta.yaml` → exit 0, SUCCESS, same output value (the `row_cell` extraction
survived the column shift), every targeted step `resolution.tier <= 2`, no `drift_flag`,
at least one step with `resolution.source == "overlay"`, and no `mask_unresolved` event
(the SSN mask resolved on beta). When `docs/decisions.md` contains a line matching
`^- P7-CUT ·` (or `ANTON_PHASE7_CUT=1` is set), `verify-6` prints a CUT notice and is
skipped by `verify-all`; prose mentions of the marker never trigger this.

**AW-7 · Risky step gating** (`make verify-7`)
(a) Replay `open_subaccount` (`--param member_id=204817 --param initial_deposit=50 --param
nickname="Holiday fund"`; the same three params in every leg) with no consent flag → exit 40,
`status == BLOCKED`, `blocked.kind == APPROVAL_REQUIRED`, `blocked.step_id == RISKY_STEP`,
run.jsonl has a `gate` event `NeedsApproval` for that step and **no** `action` event after
it, and `GET /__state/member/204817` shows no new sub-account.
(b) `--approve-risky --outputs-file <tmp>` → exit 0, SUCCESS, the outputs file's
`account_number` matches the app's `/__state` record, stdout shows `"***"` for it, and
`redacted_view` is true. (c) `INJECT=deny --approve-risky` → exit 10, `outcome_code ==
"PERMISSION_DENIED"`, `outcome_step_id == RISKY_STEP`. (d) `--param initial_deposit=10
--approve-risky` → exit 10, `outcome_code == "VALIDATION_ERROR"`, `outcome_step_id ==
steps[-1].step_id` (`click_continue_to_review` for the hand-authored artifact),
`outcome_detail` contains "at least $25". (e) A copy of the artifact with `status: draft`
(the stale `approval` block may remain; it is ignored), replayed with `--allow-draft
--approve-risky` → exit 30, FAILED, `failure.kind == POLICY_VIOLATION`, `observed_summary`
names `DRAFT_RISKY`, and no browser was launched (no screenshots in `runs/<id>/`).

**verify-evidence** (`make verify-evidence`, in `verify-all`)
Reads every seeded SSN and DOB (ISO form, as rendered) from `target_app/data.json`, plus
`demo-pass-1` and the prefix `sk-ant-`; recursively scans text files under `evidence/`,
`artifacts/`, `runs/` (never `.env*`); fails on any occurrence, printing `file:line`.

**Discovery gate (manual, not automated):** discovery is nondeterministic and needs the
author's API key, so it is verified by artifact, not by test: after phase 5 the author
checks that `evidence/discovery-run/` and `evidence/discovery-run-subaccount/` each
contain `turns.jsonl` + `run.jsonl` with model-attributed decisions and a transcript that
begins at the authenticated entry page; that for both artifacts
`provenance.discovery_run_id` equals its run's id and `approval.artifact_sha256` of the
evidence copy equals that of `artifacts/<name>.json`; that the distilled artifacts pass
AW-1, AW-1b, AW-2, AW-3, AW-4 and AW-7 **unmodified except for the approval block written
by `anton approve`**; and that the transcripts contain no unredacted sensitive values
(`verify-evidence` covers the mechanical part).

`make verify-all` runs `verify-0`, `verify-surface`, AW-1, 1b, 2, 3, 4, 5a, 6 (unless
cut), 7 and `verify-evidence`, then prints reminders for AW-5b and the discovery gate.
"verify-all green" means every non-cut target green.

**Phase mapping:** phase 1 → `verify-0`; phase 2 → `verify-target`; phase 3 → `verify-0` +
`verify-surface`; phase 4 passes
AW-1, 1b, 2, 3, 4, 7 (hand-authored artifacts); phase 5 re-passes AW-1, 1b, 2, 3, 4, 7
with the approved distilled artifacts and satisfies the discovery gate; phase 6 passes
AW-5a (AW-5b is the definition-of-done gate); phase 7 passes AW-6; phase 8 ends with
`make verify-all` green. After phase 5 the hand-authored artifacts move to
`tests/fixtures/` and are replayed by no acceptance test.

These tests are part of the deliverable, not scaffolding: commit them, and reference them
from `evidence/INDEX.md` ("each evidence folder corresponds to an acceptance workflow or
named gate").
