# REPORT

> Draft written at the author's request from what the repository actually does (every claim points at a
> file, a test, a gate or a line in `docs/decisions.md`). The author reviews and owns the final wording.

## 1. Architecture

anton is a pipeline with one seam and one choke point. A model **discovers** a flow on a legacy web
console (`anton/discovery/`), the run is **distilled** into a typed capability artifact
(`anton/distill/`), and from then on the artifact **replays with no model in the loop**
(`anton/replay/`, whose import graph is tested to contain neither `anthropic` nor the discovery package).
When replay cannot continue on its own — a locator that no longer resolves, an application error, a
checkpoint that never holds, or a risky step nobody has consented to — it **escalates** to a person
(`anton/escalation/`) in the same browser session, records what the person did, and re-localizes to
continue. Discovery and replay share the two parts that matter for safety: every action, from either, passes
`ActionGate.check` (`anton/policy/gate.py`) before it reaches the browser, and every byte written to disk or
stdout passes the `Redactor` (`anton/policy/redact.py`).

The **seam** is the `Surface` protocol (`anton/surface/base.py`): `observe` returns a redacted semantic
snapshot (roles, accessible names, text, boxes, per-frame), `act` executes one concrete action under the
step's time budget without waiting for navigation, `read` returns live text, `screenshot` returns a masked
image that fails closed. The only implementation is Playwright (`surface/web.py`), but nothing above the seam
knows that: tier-1 and tier-2 locators are resolved over snapshot geometry in `replay/locate.py`, so a
second adapter (a desktop accessibility tree, another browser driver) only has to enumerate elements with
roles, names, text and boxes. That is the answer to the brief's "heterogeneous surfaces" question, and it
is a design answer, not a demonstrated one — only the web adapter exists.

The **target app** is part of the design. `target_app/` is a Flask "Member Servicing Console" built to be
hostile the way old vendor screens are: layout by nested tables, an `<iframe name="detail">` pane holding
the member data, headings that are bold `<span>`s, a form that validates server-side, two tenants that
relabel fields, reorder the accounts table and prefix every route, and five injectable failures (session
expiry, an announcement modal, a slow page, a permission denial, a 500). Every acceptance workflow drives
the real CLI as a subprocess against a freshly spawned copy of that app; nothing in the verified path is mocked.

The build order was deliberate: the replay engine and two hand-authored artifacts came before discovery
(SPEC §17), which forced the artifact schema to be a contract that replay actually needs rather than
whatever a model happened to emit. When discovery landed, the acceptance tests were re-run unmodified
against the distilled artifacts — they pass — and the hand-authored ones were retired to `tests/fixtures/`.

## 2. Artifact schema

A capability artifact (`anton/schema/artifact.py`, ~570 lines, pydantic with `extra="forbid"` everywhere)
is a contract, not a macro. A caller can read from it what state it needs (`preconditions`, established by
named `AppProfile` flows such as `login`), what to send (`inputs: list[ParamSpec]` with types, patterns and a
`sensitive` flag), what comes back (`outputs: list[OutputSpec]` with types and normalization) and every
outcome it can produce (`business_outcomes`, a closed space: the loader refuses an artifact whose reachable
detectors emit a code that is not declared). A reviewer reads the same file through `anton inspect`, which
renders every step, every locator with its required `rationale`, the effective risk, and the outcome space.

Every polymorphic field is a discriminated union — locators by `kind`, predicates by `kind`, values by
`kind` (`literal` / `param` / `secret`), recoveries by `kind`, detectors by `arm` — so the JSON schema is
itself reviewable and an unknown field is an error rather than noise. A step's checkpoint is a
`StateSignature` (a conjunction of predicates: URL path regex, title, visible text, element visible, live
field value, checkbox state), never "hope the click worked"; `FILL`/`SELECT` steps are required to carry a
`FieldValue`/`Checked` predicate on their own target.

Element identification is a **locator ladder** with three tiers, each candidate carrying a rationale:
tier 1 is computed role plus accessible name (frame-aware); tier 2 is a relation computed over snapshot
geometry only — `right_of_label`, `below_label`, or `row_cell` (the cell at the intersection of the row
anchored by some text in a given column and the column with a given header, which is what survives a
column reorder) — never DOM parent/sibling relations; tier 3 is a normalized bounding box. A per-tier
**fingerprint** guards the resolved element: tiers 1–2 check role, control type and a name snippet; tier 3
checks role and control type only, so it genuinely survives label drift and sets `drift_flag` on the
result. Extraction ladders never use the element's own text as a name and never carry snippets — a balance
cell's "name" is its value.

Versioning is semver plus a **content hash**: `anton approve` binds `approval.artifact_sha256` to the
artifact with `status`/`approval` neutralized, so approving never moves the hash and any later edit
invalidates it (the loader refuses). `$param:` tokens are legal inside locator strings, snippets and
regexes and are expanded with the caller's original string at replay. Secrets are `SecretRef`s resolved from
`ANTON_SECRET_<NAME>` at act-time and never stored.

Discovery cannot learn what it never saw: business outcomes it did not hit, recoverable conditions that
did not occur, alternative branches. Those are declared per application by a human in the `AppProfile`
(`profiles/servicing.yaml`: the login flow, six detectors across three arms, the screenshot masks), and the
distiller inherits them into every artifact. The distiller (`distill/distiller.py`) is deterministic — the
same recorded run re-distills to a byte-identical artifact, which is what the evidence's content hashes
rest on.

## 3. Determinism & error handling

A replay ends in exactly one of four statuses (`anton/schema/result.py`): `SUCCESS` (typed outputs),
`BUSINESS_OUTCOME` (a declared code such as `MEMBER_NOT_FOUND`, with the step it fired at and the live
detail text), `BLOCKED` (a person must act or has decided: approval required, approval denied, escalation
unresolved) and `FAILED` (with a `FailureKind`: `CHECKPOINT_MISS`, `LOCATOR_EXHAUSTED`, `APP_ERROR`,
`POLICY_VIOLATION`, plus expected signature, observed summary and evidence paths). The process exit code is a
pure function of the result (0/10/40/20/30), unit-tested over every status/kind combination; `2` means no
result exists (bad params, a loader refusal). BLOCKED is deliberately not a failure: "waiting for a human"
and "the flow is broken" are different things for the calling agent.

The engine (`replay/engine.py`) is deterministic in the sense that matters: fixed step order, declarative
waits (the only sleep is a 250 ms tick), bounded retries, no branching beyond declared detectors, and one
resume path. Per step: expand params → evaluate the profile's recoverable detectors once pre-act (a known
interstitial is dismissed proactively) → resolve the ladder, polled within the step budget (the iframe pane
lands a tick after the shell) → gate → act → tick until the budget ends, evaluating in order the
postcondition, the step's detectors, the profile's detectors; the first to hold wins. A recoverable hit runs
its recovery (`Dismiss`, `RetryStep`, `RunFlow`) at most `max_attempts` times per step per run, with no
re-entrancy inside a running recovery, then **re-localizes** with `resume_point()`: it evaluates the
postconditions of every later step plus `success` and continues after the furthest one that holds. A
business detector stops cleanly at once; a hard-failure detector captures a masked screenshot and a
redacted page snapshot and fails. A step is retried only if retries remain *and* its effective risk is
SAFE; an effective-RISKY step never retries — re-clicking Confirm on an ambiguous page is the duplicate
transaction bug.

The acceptance workflows map the brief's runtime conditions to those outcomes against real injections:
record not found → `BUSINESS_OUTCOME` at `click_search` within one wait budget (AW-2); session expiry →
the `session_expired` detector runs the login flow once and the run resumes, exactly one step carrying the
recovery (AW-3a); an announcement modal → dismissed (AW-3b); a 5-second page → absorbed by the declarative
wait with one attempt and no recovery (AW-3c); a 500 → `FAILED APP_ERROR` with the expected signature,
observed summary and evidence files that exist (AW-4); validation error and permission denial → business
outcomes at the right steps (AW-7c/d). Every failing run leaves `runs/<id>/` with `run.jsonl` (one redacted
event per observation, gate decision, action, postcondition, detector, recovery, lease transition), the
redacted `result.json`, masked screenshots and the redacted page snapshot the failure references.

## 4. Heterogeneity & multi-tenant

Tenant beta of the target app relabels "Member ID" to "Customer Number", "Open Sub-Account" to "Add
Product" and "Confirm and open account" to "Complete", reorders the accounts table to `Account No. |
Balance | Type`, and prefixes every route with `/portal`. A `TenantOverlay` (`overlays/beta.yaml`) may only
**narrow or retarget** a base artifact: it can replace a step's target ladder, postcondition or timeout, add
detectors for codes already declared, override an output's extraction, set the entry URL and route prefix.
It cannot add or remove steps, change names or types, lower risk, remove a detector or introduce an outcome
code — the loader enforces all of that with artifact and overlay in hand, and retargeting a step
automatically retargets the `FieldValue` predicate that pointed at the old target.

The overlay for the balance capability retargets exactly one step. Everything else survives by construction:
the `row_cell` extraction finds the Savings balance after the column shift because it anchors on a row and a
column header rather than a position; the SSN and account-number masks resolve on beta; URL predicates,
risky-rule URL matches, the allowlist and mask scopes all see the path with the prefix stripped (a
segment-aware strip, so `/portalfoo` is not `/portal`). AW-6 proves both directions: the alpha-recorded
artifact **fails** on beta without the overlay (at `__preconditions__`, because the variant genuinely
differs) and **passes** with it, same output value, every targeted step at tier ≤ 2, `resolution.source ==
"overlay"` on the retargeted step. The bonus overlay for the sub-account capability shows the commit
button relabelled to "Complete" is still RISKY on beta through the URL rule — an overlay retargets, it
never lowers risk.

Drift is observable rather than silent: each step result records which tier and which candidate resolved
and whether the ladder came from the artifact or the overlay; a tier-3 resolve sets `drift_flag`; the
artifact records what it was recorded against (`provenance.recorded_against`). Those fields are the raw
material for cross-tenant drift analytics that were not built (see §7).

## 5. Escalation & handoff

"Who is in control" is a mechanism, not a convention. A **session lease** (`escalation/controller.py`) has
five states — `AGENT_ACTIVE`, `PAUSED`, `HUMAN_ACTIVE`, `RESUMING`, `ABANDONED` — and exactly eight edges;
the gate refuses any agent action while the lease is not `AGENT_ACTIVE` (`LEASE_NOT_HELD`), every
transition is logged, and the operator console's endpoints return 409 for anything not on the table.

Two intervention kinds cover the brief's two cases. **TAKEOVER** fires (with `--escalate`) when replay
cannot continue — ladder exhausted, hard failure, checkpoint failing after retries, an `Escalate` recovery —
and in discovery on a dead-end or the model's `ask_human`. **APPROVAL** fires when a risky step has no
consent. Both write an `InterventionRequest` (step, reason, the target's fingerprint and rationale, the
expected signature, a masked screenshot, the page signature) to `runs/<id>/intervention.json`, park the
run, start a plain Flask console in a daemon thread and print its URL. The console offers Take control /
Hand back / Abort, and for APPROVAL also Approve once / Deny. Approving grants a one-shot token bound to that
request; the engine re-runs the *same* gate with it, logs `approval_used{by: operator}`, and a second
identical action needs approval again. Denying ends replay `BLOCKED(APPROVAL_DENIED)`; in discovery the
model receives "denied by operator" and continues.

Takeover happens in the **actual headed Chromium window the run owns** — same session, same cookies. A
recorder (an init script plus an exposed binding, installed at browser-context creation so every frame and
popup is instrumented) captures the human's clicks, changes, submits and Enter presses as element
descriptors with frame paths, masked by input type and label pattern, to `human_actions.jsonl`; it is armed
only while the lease is `HUMAN_ACTIVE`. On hand-back the engine **re-localizes rather than assumes**: it
observes and calls the same `resume_point()` used by recoveries; the step whose postcondition the human
satisfied is marked `HUMAN_COMPLETED`, steps jumped over are `SKIPPED`, and if `success` already holds the
run goes straight to extraction. If nothing holds it re-escalates once, then fails with a clear detail.

The concurrency rule that makes this work: one thread owns Playwright and, while parked, loops on
`page.wait_for_timeout(250)` — an event-loop pump, not a sleep — applying the transitions the console
thread requested; the console thread never touches a browser object. The `--escalate-timeout` clock starts
at `PAUSED` and is reset by every transition and every recorded human action; expiry abandons the run.
AW-5a automates park → take → abort, the timeout, and the deny path. AW-5b — take control, click "Confirm
and open account" in the live window, hand back, run resumes to SUCCESS with the confirm step
`HUMAN_COMPLETED` — was run by the author (`evidence/escalation-demo/`), as was the discovery variant where
the operator's single "Approve once" let the model's Confirm execute and the distilled artifact carries the
risky step (`evidence/escalation-demo-discovery/`).

## 6. Safety

**Allowlist.** The effective allowlist is the policy ceiling (`anton/policy/policy.yaml`: allowed origins,
URL glob patterns where `*` is one segment and `**` any depth, allowed action types) intersected with the
application profile's own patterns, which the loader checks are covered by the policy. It is enforced twice:
before every action (an `ORIGIN`/`URL_PATTERN` deny is a `POLICY_VIOLATION`, exit 30) and after any action
that changed the URL, on every tick — a page off the allowlist is never observed or screenshotted. What it
does **not** cover is data-level access: within an allowed page the operator account can see whatever the
application shows it. That is a property of the app account, not of anton.

**Risk.** Risk is classified twice and the stricter wins: the artifact may declare a step RISKY, and the
policy applies rules over action type, URL and accessible name — the URL-of-commit rule ("any click or Enter
on `/subaccount/review` is irreversible") is the one that survives relabelling; the name rule ("Confirm…",
"Complete…") is the tenant-fragile one. Effective risk is `max(policy, step)`; a RISKY action executes only
with caller consent (`--approve-risky`) or a one-shot operator approval, never retries, and is refused
outright from an unapproved (draft) artifact — a declared risky step in a draft is refused before a browser
is launched (AW-7e). Rule-based classification is honest about its limit: it recognizes the commit screens
this app has, and a different vendor's "Submit" on a different path needs its own rule. Two guardrails
around secrets came from audits: a literal value equal to a resolved secret is denied (`SECRET_LITERAL`), and
a secret may only be typed into a credential-like field (`SECRET_TARGET`) — a conservative test keyed on the
label patterns, so an unusually labelled secret field is a false refusal until a pattern is added.

**Redaction.** The Redactor applies three rules at every sink — run logs, results, side files, stdout, the
transcript, the digest the model sees, the screenshot masks: (1) any value in the run's known-sensitive set
(resolved secrets, sensitive params, sensitive outputs once read, every `ANTON_SECRET_*` value seeded up
front), (2) value patterns (SSN shape), (3) label context — a value cell whose nearest label or column
header names a sensitive field ("SSN", "Date of Birth", "Account No.") is scrubbed at snapshot build time,
while the label itself stays so ladders still anchor on it. Screenshots mask the profile's declared elements
inside their own frame and **fail closed** to a solid image when a required mask does not resolve. The model
never handles credentials: discovery logs in through the replay engine before the first turn, and the
transcripts start at the authenticated page. Residual exposure is real: PII in free text with no label
context and no pattern is not caught; label-context scrubbing depends on the enumerator seeing the label;
masks depend on their ladders resolving (fail-closed hides the whole page, which is safe but useless); and
an image the model receives is only as clean as the masks. `make verify-evidence` proves the absence of every
seeded SSN, date of birth, the demo password and the key prefix from everything under `evidence/`,
`artifacts/` and `runs/`, text and binary.

**What the model sees, and what that rules out.** Per turn: a redacted digest of at most sixty elements
(ref, role, name, text, frame), a masked screenshot, the goal, the declared params with values only for
non-sensitive ones, the output names, and a short history. It therefore cannot pursue goals that require
reading a masked value ("find the member whose SSN is…"), typing a secret it does not hold, leaving the
allowlist, or committing a risky step without a person — and a run that needs any of those ends
`BLOCKED`/`GAVE_UP` rather than improvising.

**Limits to close next.** Approval is bound to a request, not to an identity — the console has no
authentication, so "operator" means "whoever reached localhost:7071"; the app account should be
least-privilege (today it is the demo operator); policy is URL- and name-level, not data-level; native
browser dialogs are logged and dismissed rather than reasoned about; and the allowlist is enforced in the
process, not at the network layer, so a page that loads a third-party resource is not blocked from doing so.

## 7. Cuts

- `to_tool_definition()` — emitting an approved artifact as an agent tool schema (name, typed input schema
  from `ParamSpec`, output shape, the closed outcome space as the error enum) — is the first thing to build
  next; every field it needs already exists in the artifact.
- Human TAKEOVER actions are recorded as evidence (`human_actions.jsonl`, with the same descriptor shape the
  distiller consumes) but are not distilled into artifact steps; a takeover run is always a draft that says so.
- `StateSignature.none_of` (assert an element is *absent*) and a caller-facing `max_duration` hint were not
  added; every signature is a conjunction of positive predicates.
- Human actions are recorded but not gated or annotated by policy, and the headed window shows no live
  "a person is in control" banner — the lease is visible only in the console and the log.
- Approval tokens are per intervention, not per declared step, and the `resolution` / `tenant` /
  `drift_flag` fields are recorded but no cross-tenant drift analytics consume them.
- Only the web adapter exists; the Surface seam's portability is a design claim backed by the geometry-only
  tier-2 resolution and the driver-free ladder tests, not by a second adapter.
- Nothing in the verified path was mocked: the target app, the model, the browser and the human demos are
  real. The target app itself is synthetic and deliberately small — one login, one search, one two-page
  form — so the outcome space it exercises (six detectors) is narrower than a production console's.
