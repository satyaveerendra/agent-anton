# REPORT

## 1. Architecture

- [AUTHOR: the pipeline — LLM discovery → typed capability artifact → deterministic replay → human escalation, with one policy gate (`ActionGate.check`) in front of every action in both discovery and replay, and one Redactor as the only path to disk/stdout]
- [AUTHOR: the Surface seam (`anton/surface/base.py`): everything upstream talks to `observe / act / read / resolve / screenshot`; the Playwright adapter is the only implementation, but tier-1/2 locator resolution is computed over snapshot geometry so a desktop/legacy adapter would only need to enumerate elements with roles, names, text and boxes]
- [AUTHOR: the target app as part of the design — hostile markup, an iframe pane, two tenants, five injectable failures — and why the acceptance workflows drive the real CLI against the real app with nothing mocked]
- [AUTHOR: what each phase proved and in which order (replay before discovery forced the schema to be a real contract)]

## 2. Artifact schema

- [AUTHOR: the artifact as a capability contract, not a macro — declared inputs (`ParamSpec`), typed outputs, preconditions via profile flows, a closed `business_outcomes` space enforced by the loader, versioning + content-hash approval, `inspect` as the human review surface]
- [AUTHOR: the typed locator ladder — tier 1 role+name, tier 2 label-relative / row_cell / exact text over geometry only, tier 3 normalized bbox — with a required `rationale` per candidate and a per-tier fingerprint (tiers 1–2 check name; tier 3 checks role/control_type only and sets `drift_flag`)]
- [AUTHOR: discriminated unions everywhere (locators, predicates, values, recoveries, detectors) so the JSON schema is itself reviewable; `$param:` tokens; `SecretRef` never stored]
- [AUTHOR: what discovery cannot learn (business outcomes it never hit, recoveries that did not occur) and why the outcome space and recoveries live in the human-curated `AppProfile`]

## 3. Determinism & error handling

- [AUTHOR: the four-status contract (SUCCESS / BUSINESS_OUTCOME / BLOCKED / FAILED) with `FailureKind`, and exit codes as a pure function of the result; why BLOCKED is not a failure]
- [AUTHOR: the tick loop — postcondition → step detectors → global detectors — declarative waits, bounded retries only on effective-SAFE steps, no branching beyond declared detectors, zero model calls (enforced by the purity test)]
- [AUTHOR: recoveries with `max_attempts`, no re-entrancy inside a running recovery, and the single `resume_point()` re-localization path shared with human hand-back]
- [AUTHOR: the failure signal — `FailureDetail(step, expected, observed)` + masked screenshot + redacted page snapshot; how AW-2/3/4/7 map runtime conditions to outcomes]

## 4. Heterogeneity & multi-tenant

- [AUTHOR: the overlay rules — narrow or retarget only; no new steps, no lowered risk, no new outcome codes — and what beta actually changed (labels, `/portal` prefix, column shift) vs what survived untouched (`row_cell`, masks)]
- [AUTHOR: `resolution.source` / `drift_flag` / `RecordedAgainst` as the drift signal; route_prefix applied consistently to predicates, risky rules, allowlist and masks]
- [AUTHOR: the surface abstraction as the answer to "legacy web / desktop" heterogeneity; what a second adapter would and would not have to implement]

## 5. Escalation & handoff

- [AUTHOR: the session lease (five states, eight edges) enforced by the gate (`LEASE_NOT_HELD`), so "who is in control" is a mechanism, not a convention]
- [AUTHOR: two intervention kinds — TAKEOVER (ladder exhausted, hard failure, checkpoint miss, dead-end, ask_human) and APPROVAL (a risky step without consent) — and the operator console's Approve once / Deny / Take control / Hand back / Abort]
- [AUTHOR: same live session takeover in the headed window, the human-action recorder (init script + binding, armed only while HUMAN_ACTIVE), hand-back = re-localize via `resume_point` (HUMAN_COMPLETED / SKIPPED), re-escalate once]
- [AUTHOR: the concurrency model — one Playwright-owning thread pumping `wait_for_timeout`, a console thread that only changes the lease — and the timeout clock]

## 6. Safety

- [AUTHOR: allowlist model — policy ceiling ∩ profile, where enforced (pre-action + post-navigation), what it does NOT cover (data-level access)]
- [AUTHOR: risk model — URL-of-commit + name rule, effective risk = max(policy, step), caller/operator approval, draft refusal; why rule-based classification is tenant-fragile]
- [AUTHOR: redaction — value set + patterns + label context at every sink incl. the model-bound digest; residual: unknown PII in free text, mask-resolution dependency, images]
- [AUTHOR: what the model sees and what goals that makes impossible]
- [AUTHOR: limits you would close next — identity-bound approval, least-privilege app account, data-level policy, native-dialog coverage, network-level origin blocking]

## 7. Cuts

- [AUTHOR: to_tool_definition() — emitting the artifact as an agent tool schema — first thing to build next]
- [AUTHOR: distilling human TAKEOVER actions into artifact steps (recorded as evidence today)]
- [AUTHOR: StateSignature.none_of and a caller-facing max_duration hint]
- [AUTHOR: policy annotation of human actions; a live lease banner in the window]
- [AUTHOR: per-step approval tokens and cross-tenant drift analytics from resolution/tenant fields]
