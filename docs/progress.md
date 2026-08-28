# docs/progress.md — state between sessions

## Status
- **Step 0 (housekeeping + independent review + spec revision + consistency pass): complete** — 2026-08-28.
- **Phase 1 — Scaffold + schema: complete** — 2026-08-28. Gate `make verify-0` green.
- **Phase 2 — Target app: complete** — 2026-08-28. Gate `make verify-target` green.
- **Phase 3 — Surface + policy: complete** — 2026-08-28 (1 of its 2-session budget). Gates `make verify-0` + `make verify-surface` green (output below); `verify-target` still green.
- **Phase 4 — Replay first: complete** — 2026-08-28 (1 session, on budget). Gates `make verify-1`, `verify-1b`, `verify-2`, `verify-3`, `verify-4`, `verify-7` green (output below); `verify-0`, `verify-surface`, `verify-target` still green. **Discovery spike NOT run: `ANTHROPIC_API_KEY` was not set in this session** (SPEC §17 phase 4: "skipped with a clear message if absent") — no `turns.jsonl` exists and nothing under `anton/discovery/` was written; it is the first item of phase 5.
- **Phase 5 — Discovery: not started.**

## What exists (phase 4)
- `specs/read_savings_balance.yaml`, `specs/open_subaccount.yaml` — the declared interfaces (inputs/outputs/outcome space).
- `artifacts/read_savings_balance.json` (2 steps), `artifacts/open_subaccount.json` (9 steps, one RISKY) — hand-authored per the §10.3 id rule, **approved** (`anton approve`, hash-bound); tier 1 role+name → tier 2 label-relative/exact-text → tier 3 bbox ladders, the tier-3 boxes measured on the live app at the engine's 1280×900 viewport; `inputs`/`outputs` byte-identical to the specs.
- `profiles/servicing.yaml` — login flow (Username/Password/Sign in; SecretRef `DEMO_PASSWORD`; destination-agnostic final postcondition), the six pinned detectors (`session_expired`→RunFlow login, `announcement_modal`→Dismiss, `member_not_found`/`validation_error`/`permission_denied` BUSINESS with live `detail_locator`s, `app_error` HARD on title "500"), five masks (SSN, DOB, Checking/Savings account numbers on the member page; Account Number on the confirmation page).
- `anton/replay/predicates.py` (`Evaluator`: every predicate kind; `FieldValue`/`Checked` read LIVE), `anton/replay/locate.py::resume_point` (pure, callback-based; the one resume path), `anton/replay/engine.py` (SPEC §9 verbatim: `validate_params` → draft checks before any browser → `__preconditions__` one-tick/flows/re-poll → per step pre-act RECOVERABLE detectors, ladder polled within the budget, gate, act, 250 ms tick loop postcondition→step detectors→global detectors, recoveries bounded by `max_attempts` with no re-entrancy and `resume_point` after, BUSINESS/HARD stop at once, retry only on effective-SAFE, `BLOCKED(APPROVAL_REQUIRED)` before any action, per-tick post-navigation allowlist check → `__success__` → `__outputs__` live read → regex → normalize → coerce; terminal outcomes assembled into `ReplayResult` in one place), `anton/evidence/log.py` (`RunLog`: run.jsonl through the Redactor, `observation` events, side files), `anton/api.py::invoke`, `anton replay` CLI (all §13 flags; `--escalate` exits 2 until phase 6; `--headless`/`ANTON_HEADLESS=1`).
- `Surface.read` gained `"checked"`; `Surface.last_snapshot()` added; `PlaywrightSurface.screenshot_dir` is now required.
- Tests: `tests/unit/test_engine.py` (purity via a fresh interpreter, params, coercion, `resume_point`, evaluator, the exit-30 pre-run refusal with a browser-launch tripwire, CLI usage errors, outputs-file rules), `tests/unit/test_evidence_log.py`, `tests/acceptance/test_aw_replay.py` (AW-1, 1b, 2, 3a/b/c, 4, 7a–e; artifacts selected via `ANTON_ARTIFACT_DIR`; step ids asserted structurally). Makefile: `verify-1`…`verify-7`, `replay`, `replay-notfound`, `approve`.
- 12 real run directories under `runs/` (gitignored) from the gates — the source for phase 8's curated `evidence/`.

## Verify results (phase 4, pasted from this session's terminal)
```
$ make verify-0
.venv/bin/python -m ruff check .
All checks passed!
153 passed, 43 deselected in 3.03s
$ make verify-1
1 passed, 11 deselected in 4.74s
$ make verify-1b
1 passed, 11 deselected in 2.26s
$ make verify-2
1 passed, 11 deselected in 3.00s
$ make verify-3
3 passed, 9 deselected in 13.74s
$ make verify-4
1 passed, 11 deselected in 3.23s
$ make verify-7
5 passed, 7 deselected in 13.26s
$ make verify-surface
16 passed, 180 deselected in 26.99s
$ make verify-target
15 passed in 5.20s
```
Three bounded audit agents (engine vs §9; redaction/evidence/CLI vs §5/§8/§12/§13; tests/artifacts vs §19)
found no blockers; every FIX was applied and re-verified above (decisions P4-17..P4-26). Highlights:
screenshots now mask the member page's account-number column (the snapshot side already did),
the post-navigation allowlist check runs every tick, sensitive outputs are masked by name,
`--outputs-file` is pre-flighted (never under `runs/`/`evidence/`, 0600 enforced), `--record-video`
is refused for masked profiles, tests honour `ANTON_ARTIFACT_DIR` and assert step ids structurally.

## What exists
- `SPEC.md` (revised per the accepted Step-0 review; four author pins applied; `schema/loader.py`
  added to the §3 layout), `CLAUDE.md`, `docs/decisions.md` (M/S/C/A/V items, P1-pins, P1-1..P1-15).
- Python 3.12 venv at `.venv/` (Homebrew python3.12; the system `python3` is 3.10 and must not be
  used — SPEC §1 requires 3.11+). `requirements.txt` pins pydantic 2.13.4, PyYAML 6.0.3, Flask 3.1.3,
  playwright 1.62.0, anthropic 1.2.0, pytest 9.1.1, ruff 0.16.5. `playwright install chromium` NOT yet run.
- `anton/schema/`: `artifact.py` (every §4 model as discriminated unions + validators, content hash),
  `result.py` (§5 models, arm-exclusivity validators, `exit_code()` pure function), `app_profile.py`
  (AppProfile/MaskSpec/StepOverride/TenantOverlay + login-flow rules), `snapshot.py` (§7/§10.2
  serialized models), `loader.py` (§4 loader rules 1–5, `check_flows`, overlay application with
  sources, entry-URL precedence, `load_bundle`).
- `anton/policy/gate.py`: `PolicyConfig` + `load_policy` with `${ENV:-default}` expansion; `policy.yaml` per §8.
- `anton/cli.py`: `inspect` (contract table incl. effective outcome space, static effective-risk
  bound, overlay sources, approval validity) and `approve` (content-hash binding, re-approval of a
  stale hash); `target`/`discover`/`replay` exit 2 with "not available until phase N".
- `Makefile` (`verify-0`, `verify-surface`, `test`; later targets fail loudly), `pyproject.toml`
  (ruff + pytest markers), `.env.example`, `tests/` (conftest builders, 109 unit tests).
- No application code beyond the schema layer; no `runs/` or `evidence/` content; no secrets.

## What exists (phase 3)
- `anton/surface/base.py`: `Surface` protocol (`observe/act/read/screenshot/resolve/url/path`), `ConcreteAction`
  (+`value_kind` provenance), `ResolvedElement` (live role/name + snapshot text + handle), `ActError`.
- `anton/surface/snapshot.py`: injected-JS enumeration per frame (roles from tag/type/ARIA; names from
  aria-label / label / preceding cell; static text + cells + headings + landmarks; `label_context` for
  controls AND static cells — first-row cells excepted), top-page boxes with frame offsets, `frame_path`
  segments, per-element recursive iframe visibility (batched parent hit tests), empty snapshot on a pending
  navigation, redaction applied at build.
- `anton/surface/web.py`: `PlaywrightSurface` — resolve = geometry over the redacted snapshot → live handle;
  `act` with the step budget (`no_wait_after`, `goto(commit)`, single-call SELECT) → `ActError`; live `read`;
  dialog handler (log + dismiss); screenshots with per-frame injected mask overlays, fail-closed solid PNG.
- `anton/replay/locate.py`: `$param:` expansion, tier-1/2/3 candidate resolution (row_cell survives column
  shifts incl. a moved anchor column), per-tier fingerprints, `walk_ladder` (attempts reported),
  `resolve_ladder_until` (tick polling to a deadline). `resume_point()` is owed to phase 4.
- `anton/policy/gate.py`: `ActionGate.check` (lease → action type → origin/url glob → SECRET_LITERAL →
  effective risk → DRAFT_RISKY → operator token → caller consent → NeedsApproval), `post_navigation_check`,
  `draft_check`, relative NAVIGATE URLs joined, `route_prefix`-stripped risky rules, `LeaseState` +
  the §11 edge table, `ApprovalToken`/`ApprovalStore`, `resolve_secret` (`ANTON_SECRET_<NAME>`), glob helpers.
- `anton/policy/redact.py`: `Redactor` — known values, value patterns, rule (3) over snapshot elements with
  the anchor and form-control exemptions (a mask region always wins over the anchor exemption).
- Tests: `tests/unit/test_{gate,redact,locate}.py` (31 tests), `tests/integration/test_surface.py`
  (16 Playwright tests against the real app; `make verify-surface`).
- Phase-3 audit (3 agents): no blockers; every FIX applied (decisions P3-14..P3-20).

## Verify results (phase 3, pasted from this session's terminal)
```
$ make verify-0
.venv/bin/python -m ruff check .
All checks passed!
.venv/bin/python -m pytest -q -m "not integration and not acceptance"
140 passed, 31 deselected in 2.31s
$ make verify-surface
.venv/bin/python -m pytest -q -m integration
16 passed, 155 deselected in 23.26s
$ make verify-target
15 passed
```

## What exists (phase 2)
- `target_app/`: `app.py` (Flask factory `create_app(tenant, inject, slow_ms, debug_state)`; blueprint under the
  tenant `route_prefix`; static under the prefix too; login guard with `302 /login?next=`; `?next=` carried through
  the POST; `expire`/`modal` once, `slow` every shell GET, `deny` sticky on the confirm POST, `err500` sticky on
  `GET …/subaccount/new`; `?inject=` arms process-wide; in-memory sub-accounts; `/__state/member/<id>` only under
  `ANTON_TARGET_DEBUG=1` / `--debug-state`), `seed.py` (25 fake members, canonical 204817 + 310552, ISO DOB,
  900-XX-XXXX SSNs, JSON-number balances) + committed `data.json`, hostile-markup templates (nested tables,
  `c1/row/cell`, inline styles, no ids/test-ids, `<iframe name="detail">` pane, DOM-only modal with "Dismiss"),
  `tenants/alpha.yaml` + `beta.yaml` (labels, column order, `/portal`).
- `anton target serve [--tenant] [--inject …] [--slow-ms] [--port] [--host] [--debug-state]`; `make target`.
- `tests/acceptance/conftest.py`: spawns the real CLI on a free port, cookie-jar client that never follows
  redirects, per-target log file, `Target.cli_env()` with the §19 exports for phase-4 CLI runs.
- `tests/acceptance/test_verify_target.py` (15 tests) = `make verify-target`; acceptance tests carry the
  `acceptance` marker and are excluded from `verify-0` (decisions P2-1).
- Phase-2 audit (2 agents) found no blockers; every FIX applied (decisions P2-6 revert, P2-11..P2-14).

## Verify results (phase 2, pasted from this session's terminal)
```
$ make verify-target
.venv/bin/python -m pytest -q -m acceptance tests/acceptance/test_verify_target.py
...............                                                          [100%]
15 passed in 4.02s
$ make verify-0
.venv/bin/python -m ruff check .
All checks passed!
.venv/bin/python -m pytest -q -m "not integration and not acceptance"
109 passed, 15 deselected in 2.12s
```

## Verify results (phase 1, pasted from that session's terminal)
```
$ make verify-0
.venv/bin/python -m ruff check .
All checks passed!
.venv/bin/python -m pytest -q -m "not integration"
........................................................................ [ 66%]
.....................................                                    [100%]
109 passed in 2.47s
```
An independent three-agent audit of the models against SPEC §4/§5/§7/§10.2/§16 found no blockers;
every FIX item was applied (decisions P1-11..P1-15) and re-verified above.

## Owed to later phases (tracked here so nothing is silently claimed)
- Phase-3 owed items closed in phase 4: exit-30 pre-run test (`test_draft_with_risky_step_is_refused_before_any_browser_launch`), predicate evaluator, `resume_point()`, `observation` event, `ActError` ⇒ t=0 miss, Redactor registration of secrets/sensitive outputs, `evidence/log.py`, `GateContext.route_prefix` passed by the engine. `inspect`'s effective-risk label stays a static lower bound by design (P4-15).
- **Discovery spike (SPEC §17 phase 4) not run — needs `ANTHROPIC_API_KEY`.** Phase 5 starts with it: `discovery/agent.py` loop with `act/read/finish` only on the balance goal; gate = a `turns.jsonl` in `runs/` showing the model reached the pane and cited the balance ref in `finish`.
- Phase 6 must wire: `ReplayOptions.escalate`/`escalate_timeout_s`/`console_port` (accepted, refused with exit 2 today), `LeaseState` transitions in the engine (`_gate` already passes `lease_state`; `lease` events are never emitted yet), `ApprovalStore` into `GateContext.approvals` (engine passes `None`), `BLOCKED(APPROVAL_DENIED)` (A1 still open for the author), and the human hand-back path through the same `resume_point`.
- Phase 7 (overlay): `LoadedBundle.sources` is already honoured (`resolution.source == "overlay"` for retargeted steps/outputs); AW-6 needs `overlays/servicing.beta.yaml` and the `--overlay` acceptance leg.
- The not-found page (`/member/999999`) matches the member-page masks' `applies_to`, so AW-2's screenshot fails closed to a solid PNG (P4-18) — acceptable; revisit only if phase 8 wants a readable not-found screenshot in `evidence/`.

## Deferred to phase 8
- Whether `SPEC.md` and `CLAUDE.md` remain in the public repo root or move under `docs/`
  (author decision at polish time; nothing before then depends on their location).

## Next actions
1. Author reviews phase-4 output (`git show --stat HEAD`, `make verify-1 verify-2 verify-3 verify-4 verify-7`) and says "start phase 5". Export `ANTHROPIC_API_KEY` first — phase 5 begins with the phase-4 discovery spike.
2. Phase 5 session(s): re-read SPEC §17 phase 5 and every section it references — §10 (discovery loop, tool definitions `act/read/finish`, the §10.2 turn record, the §10.3 distiller and id rule, forbidden-extraction at distill time), §12 (`turns.jsonl`, `transcript.jsonl`, `rationale` events), §13 (`discover` flags), §19 discovery gate (human-verified). The replay engine, gate, Redactor, `RunLog` and `PlaywrightSurface` are reusable as-is; discovery must gate every action through `ActionGate.check` exactly as `ReplayEngine._gate` does and log through `RunLog`. The distilled artifacts must re-pass AW-1…AW-7 via `ANTON_ARTIFACT_DIR=<distilled dir>` with the tests unmodified.
