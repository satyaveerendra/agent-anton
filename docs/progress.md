# docs/progress.md — state between sessions

## Status
- **Step 0 (housekeeping + independent review + spec revision + consistency pass): complete** — 2026-08-28.
- **Phase 1 — Scaffold + schema: complete** — 2026-08-28. Gate `make verify-0` green.
- **Phase 2 — Target app: complete** — 2026-08-28. Gate `make verify-target` green.
- **Phase 3 — Surface + policy: complete** — 2026-08-28 (1 of its 2-session budget). Gates `make verify-0` + `make verify-surface` green (output below); `verify-target` still green.
- **Phase 4 — Replay first: not started.**

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
- §16 "exit-30 pre-run case" (draft without `--allow-draft` / `DRAFT_RISKY` ⇒ FAILED POLICY_VIOLATION
  result, run dir, no browser) lands with the replay engine start-up path in phase 4
  (`ActionGate.draft_check` exists; the result/run-dir plumbing does not).
- `inspect` shows a static lower bound of effective risk; wire it to `ActionGate.effective_risk` in phase 4.
- Phase 4 must add: a predicate evaluator over snapshots (`UrlMatches`/`TitleMatches`/`TextVisible`/
  `ElementVisible`/`FieldValue` (live read)/`Checked`/`AllOf`), `resume_point()`, the `observation` event
  (P1-pin2: the pre-act observation is the snapshot the ladder resolved on), `ActError` ⇒ t=0 miss,
  secret/sensitive-output registration into the Redactor, the run logger (`evidence/log.py`).
- The gate's `route_prefix` must be passed by the engine (`GateContext.route_prefix`) — anchored risky rules
  do not match on a prefixed tenant otherwise.

## Deferred to phase 8
- Whether `SPEC.md` and `CLAUDE.md` remain in the public repo root or move under `docs/`
  (author decision at polish time; nothing before then depends on their location).

## Next actions
1. Author reviews phase-3 output (`git show --stat HEAD`, `make verify-surface`) and says "start phase 4".
2. Phase 4 session (1 session): re-read SPEC §17 phase 4 and every section it references from the file —
   §9 (start-up: param validation exit 2, loader, draft checks, launch at the effective entry URL,
   `__preconditions__` pseudo-step with one-tick poll → flows → re-poll; per-step loop: pre-act global
   RECOVERABLE detectors, ladder polled within the budget, gate, act, 250 ms tick evaluating postcondition →
   step detectors → global detectors, RECOVERABLE → recovery + `resume_point()` with no re-entrancy,
   BUSINESS → stop, HARD_FAILURE → evidence + FAILED, retry policy on EFFECTIVE risk, BLOCKED on
   NeedsApproval, escalation mode off by default; `__success__` polled; `__outputs__` live read → normalize →
   coerce; purity test), §4 (artifact/profile/overlay — hand-author `artifacts/read_savings_balance.json`
   AND `artifacts/open_subaccount.json` per the §10.3 id rule, `specs/*.yaml` first), §5 (results,
   exit codes, `--outputs-file`, redacted view), §6 (pinned labels/detector table), §8 (gate context incl.
   `route_prefix`, secrets), §12 (`observation` event schema, run dir contents), §13 (`replay` flags),
   §19 (AW-1, 1b, 2, 3a/b/c, 4, 7a–e assertions; the fixture's `Target.cli_env()` carries the exports).
   Markup facts for the artifacts: decisions P3-19 (headings are `text`/`span`, header row is `cell`);
   masks: SSN + Date of Birth cells in the pane, Account Number cell on the confirmation page.
   Gate: `make verify-1 verify-1b verify-2 verify-3 verify-4 verify-7` green, paste output, commit.
