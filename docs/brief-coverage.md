# Brief → what was built → how it is verified

A requirement-by-requirement check of the project brief against
this repository, written after `make verify-all` was green on 2026-08-28. "Verified by" names the executable
gate or the evidence folder; "Gap / note" is honest about anything thinner than the brief's wording.

## 2. The problem — the six things the system must do

| # | Brief | Built | Verified by |
|---|---|---|---|
| 1 | Take a goal in natural language for a target app | `anton discover --goal … --url …` (+ `--spec` declaring the interface, `--param`) | discovery evidence (`evidence/discovery-run*/run.jsonl` `run` event carries the goal) |
| 2 | Use an LLM to accomplish it by driving a real surface (observe → decide → act) | `anton/discovery/agent.py`: per turn a redacted digest + masked screenshot → one tool call (`act`/`read`/`finish`/`give_up`/`ask_human`) → real Playwright action through the gate | `evidence/discovery-run/` (3 turns), `evidence/discovery-run-subaccount/` (10 turns), `claude-sonnet-5` |
| 3 | Record the run as a structured, reusable, typed, versioned artifact decoupled from the transcript | `anton/distill/distiller.py` → `CapabilityArtifact` (`anton/schema/artifact.py`); no run/turn/ref is referenced by the artifact; `redistill` is byte-deterministic | `artifacts/*.json`, `tests/unit/test_distill.py`, content hash equality checked in phase 5 |
| 4 | Replay deterministically without the LLM, stable targeting, report success/failure | `anton/replay/engine.py`; purity test proves no `anthropic`/discovery import in the replay graph | AW-1, 1b, 2, 3, 4, 7 (`make verify-1 … verify-7`), `tests/unit/test_engine.py::test_replay_package_never_imports_the_llm_client` |
| 5 | Escalate to a human when stuck; take control of the live session; hand back | `anton/escalation/` (lease, console, recorder), `--escalate` in replay and discovery | AW-5a (automated), AW-5b run by the author (`evidence/escalation-demo/`), discovery approval demo (`evidence/escalation-demo-discovery/`) |
| 6 | Stay within an allowlist; never leak or persist sensitive data | `anton/policy/gate.py` (pre-action + post-navigation on every tick), `anton/policy/redact.py` at every sink, masks fail closed, `SecretRef` from env | `tests/unit/test_gate.py`, AW-4 (SSN grep + `***` positive signal), `make verify-evidence` (52 needles absent from evidence/artifacts/runs) |

## 3. Core requirements

**3.1 Goal-driven agent loop**
- goal + target as input → `--goal`, `--url` (origin-only allowed), `--param`. Verified: discovery evidence, AW-1 (`--url`).
- observe → decide → act until goal / max steps / timeout / dead-end → `max_steps=30`, 5-minute wall clock (parked time excluded), dead-end = 3 identical actions with an unchanged page signature or 6 actions without change, READ never advances. Verified: `tests/unit/test_discovery_loop.py` (fake client), real runs.
- real UI without a clean DOM → the snapshot computes roles from tag/type/ARIA and names from labels/preceding cells; no ids, test-ids or CSS selectors are used for targeting; tier 2 is geometry only; tier 3 is a bbox. The target app has nested tables, an iframe pane, bold-span headings, no test IDs. Verified: `tests/unit/test_locate.py`, `tests/integration/test_surface.py`.

**3.2 Structured artifact**
- ordered steps / how each element is identified with robustness reasoning / typed inputs / typed outputs / checkpoint → `StepSpec` list, `LocatorLadder` with a required `rationale` per candidate and per-tier fingerprint, `ParamSpec`, `OutputSpec` + coercion, per-step `postcondition` + `preconditions` + `success`. Verified: schema tests, AW-1 (`Decimal` equality on the output), `anton inspect`.
- versioned and reviewable → semver + content-hash approval (`draft` → `approved`), closed `business_outcomes`, `inspect` renders the whole contract. Verified: `tests/unit/test_loader.py`, AW-7e (draft refusal).

**3.3 Deterministic replay**
- without the LLM, stable targeting, verify checkpoint, return outputs → engine + purity test; outputs read LIVE and coerced. Verified: AW-1, AW-1b (second member + invalid-param leg → exit 2).
- runtime conditions detected and handled: validation error (AW-7d), record not found (AW-2), permission denial (AW-7c), unexpected dialog (AW-3b, plus native dialogs logged+dismissed — `tests/integration/test_surface.py`), session timeout (AW-3a), slow / failed load (AW-3c, AW-4).
- business outcome vs recoverable vs hard failure → the detector union's three arms; four result statuses (`SUCCESS` / `BUSINESS_OUTCOME` / `BLOCKED` / `FAILED` + `FailureKind`). Verified: AW-2/3/4/7, `tests/unit/test_result_exit_codes.py`.
- structured result with step / expected / observed → `FailureDetail(step_id, expected signature, observed_summary, evidence[])`. Verified: AW-4 (every evidence path exists; the page snapshot parses).

**3.4 Safety & policy guardrails**
- explicit configurable allowlist (domains/routes/action types) → `anton/policy/policy.yaml` ∩ profile patterns, `--policy`; enforced before every action and after every navigation on every tick. Verified: `tests/unit/test_gate.py`, the manual off-allowlist probe in phase 4 (exit 30).
- safe vs risky handled conservatively → effective risk = max(policy rule, declared); RISKY ⇒ `BLOCKED(APPROVAL_REQUIRED)` without consent, never retried, refused from a draft. Verified: AW-7a/b/e, AW-5a deny leg.
- never persist secrets / raw PII; redact → Redactor rules (known values, patterns, label context) at every sink incl. the model-bound digest; secrets only from env; `SECRET_LITERAL` / `SECRET_TARGET` denies. Verified: AW-4, `make verify-evidence`, `tests/unit/test_redact.py`.

**3.5 Evidence / observability** → `runs/<id>/run.jsonl` (observation, gate, action, postcondition, detector, recovery, lease, intervention, human_action events), `result.json`, masked screenshots, redacted page snapshot on failure; discovery adds `turns.jsonl`, `transcript.jsonl`, `digests/`. Verified: AW-4, `evidence/INDEX.md`.

**3.6 Human-in-the-loop**
- detect and route with context → `InterventionRequest` (capability/goal, step, reason, screenshot, target fingerprint + rationale, expected signature, page signature) written to `intervention.json`; console shows it. Verified: AW-5a.
- take control of the live session, hand back, record what the human did → same headed Chromium window, lease `HUMAN_ACTIVE`, recorder → `human_actions.jsonl`, resume via `resume_point`. Verified: AW-5b (author, PASS), `tests/integration/test_escalation_roundtrip.py`.
- know who is in control → lease state machine enforced by the gate (`LEASE_NOT_HELD`), every transition logged. Verified: AW-5a lease log, `tests/unit/test_escalation.py`.
- risky step needs a person → APPROVAL intervention, Approve once / Deny. Verified: AW-5a deny leg, discovery approval demo (author, PASS).

**3.7 Heterogeneity & scale (design)** → REPORT §4: the `Surface` seam (geometry-only tier-2 resolution, driver-free ladder tests), `TenantOverlay` with narrow/retarget-only rules, `resolution.source` / `drift_flag` / `RecordedAgainst` as the drift signal. Built beyond "design": AW-6 (tenant beta with `/portal` prefix, relabels, column shift) passes with a one-step overlay. Only the web adapter exists (stated in REPORT §7).

## 4. Explicitly your call
Language/runtime (Python 3.12, pydantic, Flask, Playwright), model (`claude-sonnet-5` via env, adaptive thinking), computer-use tech (Playwright + injected enumeration, no test IDs), target app (local hostile Flask console with synthetic data), schema, determinism strategy, architecture (single process; console in a daemon thread) — each with a `docs/decisions.md` line.
**"The discovery run has to be real"** → two real runs in `evidence/discovery-run*/` with model-attributed `rationale`s, scrubbed transcripts starting at the authenticated page, and the distilled artifacts whose `provenance.discovery_run_id` and approval hashes match. Nothing in the verified path is mocked; the target app is synthetic by design.

## 5. Scope — "a complete end-to-end vertical slice"
goal → LLM run → saved artifact → deterministic replay with params/outputs/outcomes → human escalation over the live session → evidence for both runs: every link exists and is exercised by an executable gate or an author-run demo (see above). Cuts are listed in REPORT §7.

## 6. Deliverables
- `/README.md`: setup incl. keys/config, how to run without live services ("No API key?"), the exact demo commands (discover → inspect → approve → replay → error replay → risky blocked → risky approved). ✔
- `/REPORT.md` with the seven exact headings. ✔ **Note:** the brief says ~1–3 pages; the current draft is ~3,100 words (≈6–7 pages). Trim to your voice and length before submitting.
- `/evidence/`: saved example artifacts, logs of discovery and replay runs, replays that hit errors (not-found, validation, permission denial, 500, session expiry, modal, slow) — 18 folders indexed in `evidence/INDEX.md`. ✔ No screen recording (optional).

## 8. Stretch goals (at most one or two)
- Confidence & approval — gate unattended replay on `draft → approved`: ✔ (content-hash approval, AW-7e).
- Canonicalization / cross-tenant reuse — `/member/204817 → ^/member/[^/]+$`, one artifact on a second variant with per-variant overrides: ✔ (distiller canonicalization, `overlays/beta.yaml`, AW-6).
- Not attempted: agent-facing capability catalog / `to_tool_definition()` (REPORT §7 first cut), code generation, assisted fallback, multi-run stability.

## 9. Ground rules
AI-assisted development (the author owns and must defend every decision — `docs/decisions.md` is the ledger); local app, no real site, synthetic data only; secrets out of the repo (`.env`, `runs/`, the brief are gitignored; `verify-evidence` scans for the key prefix); time-boxed by phase with `docs/progress.md` recording what was cut or deferred.
