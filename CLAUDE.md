# CLAUDE.md — operating rules for this repo

You are building the system defined in `SPEC.md`. That file is the contract; this file is
how you work on it. When the two conflict, SPEC.md wins; flag the conflict instead of
silently resolving it.

## Session protocol

- **One phase per session.** SPEC.md §17 defines phases 1–8. At session start: state which
  phase you're on, re-read SPEC.md §17 for that phase **and every SPEC section it
  references** — from the file, not from memory or summaries. The repo, `docs/decisions.md`,
  and `docs/progress.md` are the only carriers of state between sessions; assume you
  remember nothing else.
- **End every session** by updating `docs/progress.md`: phase status, verify results
  (pasted, not paraphrased), and exact next actions. Leave the repo runnable — never end
  mid-refactor.

## Gate discipline

- A phase is complete only when its `make verify-*` targets (SPEC §19 phase mapping:
  `verify-0` for phase 1, `verify-target` for phase 2, `verify-0` + `verify-surface` for
  phase 3, AW targets from phase 4) exit
  green **in this session's terminal**. Run them; paste the actual output. "Should pass,"
  "passes locally," or reasoning about why it would pass does not count.
- If a verify target fails, fixing the *test* is allowed only when the test contradicts
  SPEC.md; record any such change in `docs/decisions.md` with the SPEC citation. Never
  weaken an assertion to get to green.
- Do not start phase N+1 work while phase N gates are red, even "in parallel."
- **Phase budgets (SPEC §17) are binding.** A phase that runs a full session past its
  budget stops at the next runnable state, records the cut in `docs/decisions.md` (for
  phase 7: a line starting with `- P7-CUT ·`, which `verify-all` honours by skipping
  `verify-6`; prose mentions never count) and
  REPORT §7, and the next phase begins. Phase 7 is cut first.

## Integrity rules (non-negotiable)

- **Never fabricate evidence.** Nothing is written under `/evidence/` except by copying
  real files out of `runs/<run_id>/` from runs you actually executed. If you cannot run
  something (missing API key, needs a human for AW-5b), stop and say exactly that.
- **Never fake determinism.** No sleeps tuned to make timing tests pass, no catching
  exceptions to force exit 0, no stubbing the target app inside acceptance paths.
- **Never touch secrets.** Read `ANTHROPIC_API_KEY` from env only; never print, log, or
  commit it. If it's absent, phases 1–4 proceed (the optional end-of-phase-4 discovery spike
  is skipped with a clear message); phase 5 stops with a clear message.
- **AW-5b, the discovery gate, and the phase-6 discovery approval demo are human-verified.**
  Set them up, print the instructions, and hand off. Do not simulate the human (no
  scripted clicks in the live window, no fake `human_actions.jsonl`), and do not mark them
  passed. AW-5a is the automated half and must be green in-session like any other gate.

## Working style

- Follow SPEC.md §1 code rules: Python 3.11+, typed public functions, pydantic v2 models
  for anything serialized (discriminated unions for anything polymorphic — never a bare
  `dict`), ruff clean before every commit, small modules, stdlib bias.
- Commit at least once per phase with messages like `phase-3: surface adapter + gate
  (verify: schema, gate, redaction green)`.
- Every judgment call SPEC.md leaves open gets one line in `docs/decisions.md`:
  *decision — alternative considered — why*. These feed the author's REPORT.md; write
  them as an engineer defending a choice, not as changelog noise.
- Ask the author (stop and print a question) when: a SPEC ambiguity changes a schema or
  contract, a dependency beyond SPEC §2 seems needed, or any acceptance assertion looks
  wrong. Guess-and-continue is for cosmetic choices only.
- REPORT.md: skeleton + `[AUTHOR: ...]` prompts only (SPEC §15). Do not write finished
  prose in the author's voice.

## Definition of done (final session)

`make verify-all` green — every non-cut target (`verify-0`, `verify-surface`, AW-1, 1b, 2,
3, 4, 5a, 6 unless cut, 7 + `verify-evidence`) · AW-5b + the discovery gate + the phase-6 discovery approval demo
confirmed by the author · `evidence/INDEX.md` maps every folder to its AW-N or named gate · SPEC §18 coverage table re-checked
against the actual repo · README demo path executed top-to-bottom in a fresh venv ·
no ruff errors · no TODOs in committed code that aren't mirrored in REPORT.md's Cuts
prompts.
