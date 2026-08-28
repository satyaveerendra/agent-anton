# docs/progress.md — state between sessions

## Status
- **Step 0 (housekeeping + independent review + spec revision + consistency pass): complete** — 2026-08-28.
- **Phase 1 — Scaffold + schema: complete** — 2026-08-28. Gate `make verify-0` green.
- **Phase 2 — Target app: complete** — 2026-08-28. Gate `make verify-target` green.
- **Phase 3 — Surface + policy: complete** — 2026-08-28 (1 of its 2-session budget). Gates `make verify-0` + `make verify-surface` green (output below); `verify-target` still green.
- **Phase 4 — Replay first: complete** — 2026-08-28 (1 session, on budget). Gates `make verify-1`, `verify-1b`, `verify-2`, `verify-3`, `verify-4`, `verify-7` green (output below); `verify-0`, `verify-surface`, `verify-target` still green. **Discovery spike run and gate met** (after the author supplied the key later in the session): `runs/20260828T173306Z-a7898d/turns.jsonl` — 3 turns with `claude-sonnet-5`, the model reached the member pane and `finish` cited ref `e24` (the Savings row's Balance cell in `iframe[name=detail]`, harness-read as `$19,823.28` = the seeded value); every `act` has a matching `gate` event; no key/password/SSN anywhere in the run tree.
- **Phase 5 — Discovery + distiller: complete** — 2026-08-28 (1 session, on budget). Real discovery runs with `claude-sonnet-5`: balance goal (3 turns) and sub-account goal with `--approve-risky` (10 turns); both distilled → `inspect` → `approve`; the distilled artifacts re-pass AW-1, 1b, 2, 3, 4 and 7 **unmodified** (output below). Discovery gate: evidence curated (`evidence/INDEX.md`); the human-verified checks are listed under "Next actions" for the author. Three bounded audits (loop/distiller vs §10, tests vs §16/§19, security of the discovery path) found no design blockers; every FIX applied (P5-14..P5-23), runs regenerated with the final code.
- **Phase 6 — Escalation: automated half complete** — 2026-08-28 (session 1 of 2). `make verify-5a` green (park/take/abort, timeout, deny); two bounded audits (escalation vs §11; tests vs §16/§19) — one blocker (a park-loop race, P6-11) and every FIX applied; the AW-5b round-trip MECHANICS pass automatically (`tests/integration/test_escalation_roundtrip.py`); **AW-5b itself and the discovery approval demo are human-run and still owed** — see "Next actions".
- **Phase 7 — Overlay: not started.**

## What exists (phase 6)
- `anton/escalation/controller.py` — `Lease` (lock-protected §11 state machine, every transition logged as a `lease` event, edges cross-checked against `LEASE_TRANSITIONS`), `EscalationController` (pending `InterventionRequest` → `intervention.json`, console start, stderr URL, the park loop on `page.wait_for_timeout(250)` pumps, `--escalate-timeout` clock reset on transitions and human actions, one-shot `ApprovalStore`, recorder armed flag, `human_actions.jsonl` through the Redactor, `InterventionRecord`s).
- `anton/escalation/operator_app.py` — the console (`GET /`, `/api/state`, `/api/take|handback|approve|deny|abort`, `/screenshot`; 409 on invalid transitions; approve/deny only for APPROVAL) served by `werkzeug.make_server` in a daemon thread that never touches Playwright.
- `anton/escalation/recorder.py` — init script + `__anton_record` binding installed at context creation; idempotent; click/change/submit/Enter as descriptors with frame path; password/name-pattern masking.
- Engine: `--escalate` wired (APPROVAL on `NeedsApproval`, TAKEOVER on ladder exhausted / HARD_FAILURE / checkpoint miss / Escalate recovery / `__success__` miss), operator approval consumed by the same gate (`approval_used{by: operator}`), deny ⇒ `BLOCKED(APPROVAL_DENIED)`, abort/timeout ⇒ `BLOCKED(ESCALATION_UNRESOLVED)` with `intervention_request_path`, hand-back ⇒ `resume_point` (HUMAN_COMPLETED, `resumed_at_step`), re-escalate once then FAILED. Discovery: APPROVAL / TAKEOVER interventions, "denied by operator" tool result, `human_takeover` ⇒ draft note.
- Tests: `tests/unit/test_escalation.py` (lease table, console 409s, timeout, take/handback with masked human actions, engine triggers with a fake surface: APPROVAL / TAKEOVER × LOCATOR_EXHAUSTED / CHECKPOINT_MISS / APP_ERROR, no further gate calls, approve-once, hand-back resume, deny, re-escalate-once), `tests/acceptance/test_aw5a.py` (three legs), `tests/integration/test_escalation_roundtrip.py`; scripts `tests/acceptance/verify_5b.py` (`make verify-5b`) and `tests/acceptance/demo_discovery_approval.py` (`make demo-discovery-approval`).

## Verify results (phase 6, pasted from this session's terminal)
```
$ make verify-0        All checks passed!   191 passed, 47 deselected   (final, after audit fixes P6-11..P6-18)
$ make verify-5a       3 passed in 15.68s
$ pytest -m integration tests/integration/test_escalation_roundtrip.py   1 passed in 3.96s
$ make verify-1/1b/2/3/4/7   1 / 1 / 1 / 3 / 1 / 5 passed     verify-surface 17 passed    verify-target 15 passed
```

## What exists (phase 5)
- `anton/schema/spec.py` (`CapabilitySpec`: inputs + outputs minus extraction, loaded from `specs/*.yaml`), `anton/discovery/describe.py` (element descriptors from snapshot geometry: label context, table header row, row anchor + its column header), `anton/discovery/agent.py` (the §10.1 loop: login through the replay engine, digest ≤60 + masked screenshot, tools act/read/finish/give_up/ask_human, every act gated, per-tick post-navigation check, dead-end rule, `TurnRecord` per turn, scrubbed `transcript.jsonl`, `digests/`, `discovery.json`, `DiscoveryResult`), `anton/distill/distiller.py` (§10.3 rules 1–12 + `redistill` from a run dir), `anton discover` CLI (all §13 flags; `--escalate` exits 2 until phase 6), Makefile `discover` / `discover-subaccount`.
- `artifacts/read_savings_balance.json` (distilled from run `20260828T192716Z-14d564`, 2 steps) and `artifacts/open_subaccount.json` (run `20260828T192729Z-07ca25`, 8 steps incl. the RISKY confirm click taken from the gate decision), both approved; the hand-authored ones moved to `tests/fixtures/`.
- Tests: `tests/unit/test_distill.py` (binding incl. `$50.00`, canonicalization, ids, ladders/extraction rule, landmark + title fallback, Checked/RISKY, secrets, redistill determinism), `tests/unit/test_discovery_loop.py` (fake client + fake surface: dead-end 3/6, READ never advances, ask_human → give_up, BLOCKED without consent, finish → loadable draft), `tests/unit/test_cli.py` updated.
- `evidence/` — `discovery-run/`, `discovery-run-subaccount/`, `artifact/`, `INDEX.md`.

## Verify results (phase 5, pasted from this session's terminal — distilled artifacts under test)
```
$ make verify-0        All checks passed!   179 passed, 43 deselected   (final)
$ make verify-1   1 passed    $ make verify-1b  1 passed    $ make verify-2  1 passed
$ make verify-3   3 passed    $ make verify-4   1 passed    $ make verify-7  5 passed
$ make verify-surface  16 passed        $ make verify-target  15 passed
$ anton discover … read_savings_balance  → SUCCESS, 3 turns, run 20260828T192716Z-14d564  (--out artifacts)
$ anton discover … open_subaccount --approve-risky → SUCCESS, 10 turns, run 20260828T192729Z-07ca25
$ redistill(run) content hash == artifacts/*.json content hash: True / True; approval bound: True / True
```

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
- Discovery spike done (`anton/discovery/agent.py`, `python -m anton.discovery.agent --url … --param member_id=…`): act/read/finish only, login through the replay engine, every act gated, `turns.jsonl` + scrubbed `transcript.jsonl` + per-turn digests. Phase 5 turns it into the real module: `--spec` interface, `give_up`/`ask_human`, dead-end rule exactly per §10.1, `label_context`/`row_anchor`/`col_header` on descriptors (the spike records them as None), the distiller (§10.3), `anton discover` CLI, and the human-verified discovery gate.
- Phase 6 must wire: `ReplayOptions.escalate`/`escalate_timeout_s`/`console_port` (accepted, refused with exit 2 today), `LeaseState` transitions in the engine (`_gate` already passes `lease_state`; `lease` events are never emitted yet), `ApprovalStore` into `GateContext.approvals` (engine passes `None`), `BLOCKED(APPROVAL_DENIED)` (A1 still open for the author), and the human hand-back path through the same `resume_point`.
- Phase 7 (overlay): `LoadedBundle.sources` is already honoured (`resolution.source == "overlay"` for retargeted steps/outputs); AW-6 needs `overlays/servicing.beta.yaml` and the `--overlay` acceptance leg.
- The not-found page (`/member/999999`) matches the member-page masks' `applies_to`, so AW-2's screenshot fails closed to a solid PNG (P4-18) — acceptable; revisit only if phase 8 wants a readable not-found screenshot in `evidence/`.

## Deferred to phase 8
- Whether `SPEC.md` and `CLAUDE.md` remain in the public repo root or move under `docs/`
  (author decision at polish time; nothing before then depends on their location).

## Next actions
1. **AW-5b (human, definition of done):** `make verify-5b` — the script spawns the app, starts the HEADED replay of `open_subaccount --escalate` without consent and prints the console URL when it parks. You: open the console → **Take control** → click **Confirm and open account** in the Chromium window → **Hand back**. The script prints PASS/FAIL against SPEC §19 AW-5b and the run dir to curate into `evidence/escalation-demo/`.
2. **Discovery approval demo (human):** `make demo-discovery-approval` — `discover` on the sub-account goal with `--escalate`, no `--approve-risky`; when it parks, click **Approve once** in the console; the script checks the model's Confirm executed, `approval_used{by: operator}`, and the distilled artifact carries the RISKY step; curate the run into `evidence/escalation-demo-discovery/`.
3. Discovery gate checks from phase 5 (unchanged), A1 (now implemented as `BLOCKED(APPROVAL_DENIED)` — confirm), A12, A13.
4. Then "start phase 7" (overlay, AW-6) — the loader/engine already honour `LoadedBundle.sources`; phase 7 writes `overlays/servicing.beta.yaml` and the AW-6 leg.
