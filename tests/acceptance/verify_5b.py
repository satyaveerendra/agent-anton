"""SPEC §19 AW-5b — the human escalation round-trip (definition of done; run by the author).

    make verify-5b

Spawns the target app, starts `anton replay open_subaccount --escalate` HEADED without consent, and
waits. YOU: open the console URL printed below, click "Take control", click "Confirm and open
account" in the Chromium window, then click "Hand back". The script asserts the SPEC §19 AW-5b
conditions and prints PASS / FAIL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from anton.schema.artifact import CapabilityArtifact, RiskClass  # noqa: E402
from anton.schema.loader import repo_root  # noqa: E402
from tests.acceptance.conftest import free_port, spawn_target  # noqa: E402

ART_DIR = os.environ.get("ANTON_ARTIFACT_DIR", "artifacts")
SUBACCOUNT = f"{ART_DIR}/open_subaccount.json"


def main() -> int:
    art = CapabilityArtifact.model_validate_json((repo_root() / SUBACCOUNT).read_text(encoding="utf-8"))
    risky = [s.id for s in art.steps if s.risk is RiskClass.RISKY]
    assert len(risky) == 1, risky
    risky_step = risky[0]
    target = spawn_target("alpha")
    port = free_port()
    run_dir = Path(tempfile.mkdtemp(prefix="anton-5b-")) / "run"
    outputs = run_dir.parent / "outputs.json"
    try:
        before = json.loads(target.client().get("/__state/member/204817").body)
        cmd = [sys.executable, "-m", "anton", "replay", SUBACCOUNT, "--param", "member_id=204817", "--param", "initial_deposit=50", "--param", "nickname=Holiday fund",
               "--url", target.origin, "--escalate", "--escalate-timeout", "900", "--console-port", str(port), "--run-dir", str(run_dir), "--outputs-file", str(outputs)]
        env = {**target.cli_env()}
        env.pop("ANTON_HEADLESS", None)  # HEADED: the human drives this window
        print(f"\n[verify-5b] starting headed replay; run dir {run_dir}", flush=True)
        proc = subprocess.Popen(cmd, cwd=repo_root(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None and not (run_dir / "intervention.json").exists():
            time.sleep(0.5)
        if proc.poll() is not None:
            out, err = proc.communicate()
            print("FAIL: the run ended before parking:\n" + err, flush=True)
            return 1
        print(f"\n[verify-5b] PARKED. Now, in your browser:\n  1. open http://127.0.0.1:{port}\n  2. click 'Take control'\n  3. in the Chromium window the run owns, click 'Confirm and open account'\n  4. back in the console, click 'Hand back'\n  (timeout 900 s)\n", flush=True)
        try:
            out, err = proc.communicate(timeout=1000)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            proc.communicate()
            print("\nFAIL: the run did not finish (timeout / interrupted); killed the CLI, browser and console.")
            return 1
        result = json.loads(out) if out.strip().startswith("{") else None
        after = json.loads(target.client().get("/__state/member/204817").body)
    finally:
        if "proc" in locals() and proc.poll() is None:
            proc.kill()
        target.stop()
    checks: list[tuple[str, bool]] = []
    checks.append(("exit 0", proc.returncode == 0))
    checks.append(("status SUCCESS", bool(result) and result["status"] == "SUCCESS"))
    raw = json.loads(outputs.read_text(encoding="utf-8")) if outputs.exists() else {}
    acct = raw.get("account_number")
    checks.append(("outputs file account_number matches /__state", bool(acct) and any(str(a.get("account_no")) == acct for a in after["accounts"] + after["runtime_created"]) and len(after["accounts"]) == len(before["accounts"]) + 1))
    checks.append(("stdout shows *** for account_number", bool(result) and result.get("outputs", {}).get("account_number") == "***"))
    iv = (result or {}).get("interventions") or [{}]
    checks.append(("interventions[0].kind == APPROVAL", iv[0].get("kind") == "APPROVAL"))
    checks.append(("resolution == resumed", iv[0].get("resolution") == "resumed"))
    checks.append(("human_action_count >= 1", (iv[0].get("human_action_count") or 0) >= 1))
    checks.append(("resumed_at_step == __outputs__", iv[0].get("resumed_at_step") == "__outputs__"))
    step = next((s for s in (result or {}).get("steps", []) if s["step_id"] == risky_step), {})
    checks.append((f"{risky_step} status HUMAN_COMPLETED", step.get("status") == "HUMAN_COMPLETED"))
    ev = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()] if (run_dir / "run.jsonl").exists() else []
    lease = [(e["from"], e["to"]) for e in ev if e["kind"] == "lease"]
    checks.append(("lease log AGENT_ACTIVE→PAUSED→HUMAN_ACTIVE→RESUMING→AGENT_ACTIVE", lease == [("AGENT_ACTIVE", "PAUSED"), ("PAUSED", "HUMAN_ACTIVE"), ("HUMAN_ACTIVE", "RESUMING"), ("RESUMING", "AGENT_ACTIVE")]))
    seqs = {e["to"]: e["seq"] for e in ev if e["kind"] == "lease"}
    human_window = [e for e in ev if e["kind"] == "action" and seqs.get("HUMAN_ACTIVE", 0) < e["seq"] < seqs.get("RESUMING", 10**9)]
    checks.append(("no action events gated through during HUMAN_ACTIVE", not human_window))
    checks.append(("human_actions.jsonl written", (run_dir / "human_actions.jsonl").exists()))
    ok = all(v for _, v in checks)
    for name, v in checks:
        print(f"  [{'ok' if v else 'FAIL'}] {name}")
    print(f"\nAW-5b: {'PASS' if ok else 'FAIL'}   (run dir {run_dir}; curate it into evidence/escalation-demo/)")
    if not ok:
        print(err[-2000:])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
