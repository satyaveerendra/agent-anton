# anton — a digital back-office operator

anton learns a browser flow once and replays it exactly: a model discovers a legacy back-office UI, the
run is distilled into a **typed, reviewable capability artifact**, and from then on the artifact replays
**deterministically with no model in the loop** — with a policy gate in front of every action, redaction at
every sink, a closed set of business outcomes, and a human escalation path (lease, operator console,
takeover, hand-back) when the run cannot continue on its own.

The demo target is a deliberately hostile "Member Servicing Console" (nested tables, an iframe pane, two
tenants, injectable failures) that ships in this repo. Everything is synthetic; see *Fake credentials* below.

## Setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate      # 3.11+ works; the venv here is 3.12
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                                   # only `anton discover` needs ANTHROPIC_API_KEY
```

`.env` is gitignored and loaded automatically by `python -m anton …`; real environment variables always
win. `ANTON_MODEL` defaults to `claude-sonnet-5`.

## The demo path (in order)

Every command below is a thin wrapper over `python -m anton …`; run the app in one terminal first.

```bash
make target                                  # 1. serve the Member Servicing Console on http://localhost:8000  (TENANT=beta INJECT=expire,modal …)
make discover OUT=runs/demo                  # 2. discover the balance capability  →  runs/demo/read_savings_balance.json (draft)
                                             #    (OUT defaults to artifacts/, which would overwrite the committed, evidence-linked approved artifact;
                                             #     `git checkout artifacts/` restores it)
python -m anton inspect runs/demo/read_savings_balance.json   # 3. review the contract: inputs, outputs, closed outcome space, every locator + rationale
python -m anton approve runs/demo/read_savings_balance.json   # 4. draft → approved (content-hash bound; any later edit invalidates it)
python -m anton replay runs/demo/read_savings_balance.json --param member_id=204817   # 5. deterministic replay of what you just discovered
make replay                                  # 5'. the same with the committed approved artifact, exit 0, typed outputs   (MEMBER=310552 for another member)
make replay-notfound                         # 6. error replay: BUSINESS_OUTCOME MEMBER_NOT_FOUND, exit 10 — an outcome, not a crash
python -m anton replay artifacts/open_subaccount.json \
  --param member_id=204817 --param initial_deposit=50 --param nickname="Holiday fund"        # 7. risky step → BLOCKED(APPROVAL_REQUIRED), exit 40
python -m anton replay artifacts/open_subaccount.json \
  --param member_id=204817 --param initial_deposit=50 --param nickname="Holiday fund" \
  --approve-risky --outputs-file /tmp/out.json                                               # 8. risky step approved by the caller → SUCCESS; raw output only in the 0600 file
```

Other things worth trying: `make replay-beta` (the alpha-recorded artifact on tenant beta through
`overlays/beta.yaml`, `make target TENANT=beta`), `make discover-subaccount`, and escalation:
`python -m anton replay artifacts/open_subaccount.json … --escalate` parks at the risky step and prints the
operator console URL (default `http://127.0.0.1:7071`, `--console-port` to change it; note macOS AirPlay
occupies 5000/7000) — Approve once / Deny / Take control / Hand back / Abort.

Every run writes `runs/<run_id>/` (`run.jsonl` through the Redactor, `result.json` redacted view, masked
screenshots, redacted page snapshots on failure). Exit codes are a pure function of the result: `0`
SUCCESS · `10` BUSINESS_OUTCOME · `20` FAILED · `30` FAILED/POLICY_VIOLATION · `40` BLOCKED · `2` usage.

## Call it from Python

```python
from anton.api import invoke

result = invoke("artifacts/read_savings_balance.json", {"member_id": "204817"}, url="http://localhost:8000")
print(result.status, result.outputs)          # SUCCESS {'savings_balance': 19823.28}
```

`invoke(...)` takes the same options as the CLI (`overlay`, `policy`, `approve_risky`, `allow_draft`,
`escalate`, `outputs_file`, …) and returns the redacted `ReplayResult`.

## No API key?

Everything except `discover` works without one. Two **approved, discovered** artifacts are committed
(`artifacts/read_savings_balance.json`, `artifacts/open_subaccount.json`, both distilled from real runs
whose evidence is in `evidence/`), so you can go straight to steps 5–8 above, and run every automated gate:

```bash
make verify-all          # unit + surface + AW-1, 1b, 2, 3, 4, 5a, 6, 7 + the evidence scan; then reminders for the two human gates
make verify-5b           # AW-5b, the human round-trip (take control → click Confirm in the live window → hand back)
```

## Fake credentials

The target app's operator login is `ops_user` / `demo-pass-1` — a fake demo credential seeded in
`target_app/data.json`. Replay never types it as a literal: the login flow uses `SecretRef("DEMO_PASSWORD")`,
resolved at act-time from `ANTON_SECRET_DEMO_PASSWORD` (set in `.env.example`); the model never sees it
(discovery logs in through the replay engine before the first turn). Every SSN, date of birth and account
number in the seed is synthetic (`900-XX-XXXX` is an invalid SSN range) and is scrubbed from every log,
snapshot and transcript, and masked in every screenshot; `make verify-evidence` proves the absence from every
file under `evidence/`, `artifacts/` and `runs/` (text files line by line, binaries by byte scan), and AW-4
additionally byte-scans the screenshots of a failing run.

## Where to look

- `evidence/INDEX.md` — every evidence folder mapped to the acceptance workflow or gate it demonstrates (discovery runs with transcripts, the escalation demos, one replay folder per AW).
- `SPEC.md` — the contract the code was built to; `REPORT.md` — the design write-up; `docs/decisions.md` — every judgment call, one line each.
- `anton/schema/artifact.py` — the artifact schema (the centerpiece); `anton/policy/` — the gate and the Redactor; `anton/replay/engine.py` — the deterministic executor; `anton/escalation/` — lease, console, recorder.
