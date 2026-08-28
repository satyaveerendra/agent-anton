# docs/progress.md — state between sessions

## Status
- **Step 0 (housekeeping + independent review + spec revision + consistency pass): complete** — 2026-08-28.
- **Phase 1 — Scaffold + schema: complete** — 2026-08-28. Gate `make verify-0` green (output below).
- **Phase 2 — Target app: not started.**

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

## Verify results (phase 1, pasted from this session's terminal)
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
  result, run dir, no browser) lands with the replay engine start-up path in phase 4.
- `inspect` shows a static lower bound of effective risk; the full act-time computation needs the
  ActionGate (phase 3) — then wire `inspect` to it.
- `ResolvedElement`, `ConcreteAction`, `ActError` land in `anton/surface/base.py` (phase 3).

## Deferred to phase 8
- Whether `SPEC.md` and `CLAUDE.md` remain in the public repo root or move under `docs/`
  (author decision at polish time; nothing before then depends on their location).

## Next actions
1. Author reviews phase-1 output (`git show --stat HEAD`, `make verify-0`) and says "start phase 2".
2. Phase 2 session: re-read SPEC §17 phase 2 and every section it references from the file —
   §6 (pages, pinned labels, "Sign out" landmark, 302/`?next=`, `novalidate` form, in-memory
   sub-accounts + `/__state` under `ANTON_TARGET_DEBUG=1`, injection table, tenant variants, seed with
   canonical ids 204817/310552, ISO DOB), §13 (`target serve --tenant/--inject`), §19 `verify-target`
   contents; build `target_app/` (Flask, hostile markup, iframe pane), `seed.py`, `tenants/*.yaml`,
   the `verify-target` curl script + Makefile target; run `make verify-target`, paste output, commit.
