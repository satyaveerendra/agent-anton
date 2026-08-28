# Evidence index

Each folder corresponds to an acceptance workflow (AW) or a named gate in SPEC §19. Every file was
produced by a real run of the code in this repository against the local target app; run.jsonl,
turns.jsonl, transcript.jsonl and the digests went through the Redactor before they were written.

| folder | gate | what it shows |
|---|---|---|
| `discovery-run/` | discovery gate | `anton discover` on the balance goal (run `20260828T192716Z-14d564`, `claude-sonnet-5`, 3 turns): `turns.jsonl` (model-attributed decisions), `run.jsonl` (login through the replay engine, every act gated), `transcript.jsonl` (scrubbed request/response payloads; begins at the authenticated entry page), `digests/` (the redacted snapshot per turn), masked screenshots, `discovery.json` (the distiller's inputs). |
| `discovery-run-subaccount/` | discovery gate | the risky sub-account goal with `--approve-risky` (run `20260828T192729Z-07ca25`, 10 turns): the confirm click is recorded as `Allow:RISKY` from the gate and becomes the artifact's RISKY step. |
| `artifact/read_savings_balance.json` | discovery gate | the approved distilled artifact — the run's own `--out artifacts` output (its `result.json` names `artifacts/read_savings_balance.json`), approved with `anton approve`; `provenance.discovery_run_id` = the run above; same `approval.artifact_sha256` as `artifacts/read_savings_balance.json`. `anton.distill.redistill(run_dir, …)` reproduces it byte-for-byte (content hash) from `turns.jsonl` + `digests/` + `discovery.json`. Re-passes AW-1, 1b, 2, 3, 4 unmodified. |
| `artifact/open_subaccount.json` | discovery gate | the approved distilled artifact for the sub-account goal — likewise the run's own output, reproducible with `redistill`. Re-passes AW-7 (and AW-4) unmodified. |

| `escalation-demo/` | AW-5b (human, definition of done) | `anton replay open_subaccount --escalate` without consent, HEADED: parked at the RISKY confirm step (`intervention.json`, kind APPROVAL); the author clicked **Take control**, clicked **Confirm and open account** in the live window (`human_actions.jsonl`, `origin: "human"`), clicked **Hand back**; the run re-localized and resumed to SUCCESS — `result.json` shows the confirm step `HUMAN_COMPLETED`, `interventions[0]` resumed with `resumed_at_step: "__outputs__"`, and `run.jsonl` the lease log `AGENT_ACTIVE→PAUSED→HUMAN_ACTIVE→RESUMING→AGENT_ACTIVE` with no agent `action` during `HUMAN_ACTIVE`. Run by the author on 2026-08-28; `tests/acceptance/verify_5b.py` printed PASS. |
| `escalation-demo-discovery/` | phase-6 discovery approval demo (human) | `anton discover` on the sub-account goal with `--escalate` and no `--approve-risky` (run `20260828T220916Z-b2e673`): the model's confirm click raised an APPROVAL intervention; the author clicked **Approve once**; the one-shot token was consumed by the gate (`approval_used{by: operator}`), the click executed (`turns.jsonl` turn recorded `Allow:RISKY;operator`), and the distilled artifact carries the RISKY step. |

| `replay-tenant-beta/` | AW-6 | the alpha-recorded `read_savings_balance` artifact replayed on tenant beta through `overlays/beta.yaml`: `result.json` shows `tenant_id: beta`, `overlay_id: beta`, `fill_member_id` resolved at tier 1 with `resolution.source: "overlay"` ("Customer Number"), the `row_cell` extraction surviving the column shift (same balance as alpha), no `mask_unresolved` in `run.jsonl`. |

Replay evidence folders (`replay-success/`, `replay-not-found/`, …) are curated in phase 8 from the
acceptance runs under `runs/`.
