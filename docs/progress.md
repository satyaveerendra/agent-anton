# docs/progress.md — state between sessions

## Status
- **Step 0 (housekeeping + independent review + spec revision + consistency pass): complete** — 2026-08-28.
- **Phase 1: not started.** Do not start until the author says so.

## What exists
- `SPEC.md` revised per the accepted Step-0 review (all approved M/S/C items applied;
  §17 phases, §18 coverage map, §19 AW set regenerated), then two consistency passes
  (decisions V1–V20, then V21–V28) and a final focused re-check: every §19 assertion is
  satisfiable by the schema and mechanics as written (see the Step-0 report).
- `CLAUDE.md` updated (end-of-phase-4 spike skip rule, AW-5a/5b split, phase budgets +
  `- P7-CUT ·` marker, union rule, phase 1–3 gates `verify-0`/`verify-target`/`verify-surface`, DoD).
- `docs/decisions.md` records every applied item, derivative assumptions A1–A11
  (A1 pending author confirmation), and the declined items.
- `docs/brief.pdf` present locally, gitignored. No application code. No commits yet.

## Verify results
- None yet (no `make` targets exist before phase 1).

## Deferred to phase 8
- Whether `SPEC.md` and `CLAUDE.md` remain in the public repo root or move under `docs/`
  (author decision at polish time; nothing before then depends on their location).

## Next actions
1. Author confirms/vetoes A1–A11 (A1 = operator Deny semantics on replay).
2. Author says "start phase 1" → session reads SPEC §17 phase 1 and every section it
   references (§1, §3, §4, §5, §7 models, §10.2 models, §13 `inspect`/`approve`, §16 schema
   + exit-code tests, §19 `verify-0`), builds scaffold + all schema models (incl. the
   §7/§10.2 serialized ones) + `inspect` + `approve` + exit-code mapping, runs
   `make verify-0`, pastes output.
