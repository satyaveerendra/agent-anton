"""Phase-6 human demo (SPEC §17): `discover` on the sub-account goal with --escalate and NO
--approve-risky → APPROVAL intervention → the operator clicks "Approve once" → the model's Confirm
executes → the distilled artifact carries the RISKY step. Run by the author:

    make demo-discovery-approval
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from anton.schema.artifact import CapabilityArtifact, RiskClass  # noqa: E402
from anton.schema.loader import repo_root  # noqa: E402
from tests.acceptance.conftest import free_port, spawn_target  # noqa: E402


def main() -> int:
    target = spawn_target("alpha")
    port = free_port()
    out_dir = Path(tempfile.mkdtemp(prefix="anton-demo-")) / "artifacts"
    try:
        cmd = [sys.executable, "-m", "anton", "discover", "--goal", "Open a new savings sub-account with a $50 initial deposit for member 204817 and reach the confirmation screen",
               "--profile", "profiles/servicing.yaml", "--spec", "specs/open_subaccount.yaml", "--param", "member_id=204817", "--param", "initial_deposit=50",
               "--param", "nickname=Holiday fund", "--url", target.origin, "--out", str(out_dir), "--escalate", "--escalate-timeout", "900", "--console-port", str(port)]
        env = {**target.cli_env()}
        env.pop("ANTON_HEADLESS", None)
        print(f"\n[demo] discovery with --escalate (no --approve-risky). When it parks, open http://127.0.0.1:{port} and click 'Approve once'.\n", flush=True)
        proc = subprocess.Popen(cmd, cwd=repo_root(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(timeout=1500)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            proc.communicate()
            print("\nFAIL: discovery did not finish (timeout / interrupted); killed the CLI, browser and console.")
            return 1
        result = json.loads(out) if out.strip().startswith("{") else None
    finally:
        if "proc" in locals() and proc.poll() is None:
            proc.kill()
        target.stop()
        time.sleep(0.2)
    checks = [("exit 0 / SUCCESS", proc.returncode == 0 and bool(result) and result["status"] == "SUCCESS")]
    iv = (result or {}).get("interventions") or [{}]
    checks.append(("interventions[0].kind == APPROVAL, resolution approved", iv[0].get("kind") == "APPROVAL" and iv[0].get("resolution") == "approved"))
    art_path = (result or {}).get("artifact_path")
    art = CapabilityArtifact.model_validate_json((repo_root() / art_path).read_text(encoding="utf-8")) if art_path and (repo_root() / art_path).exists() else (CapabilityArtifact.model_validate_json(Path(art_path).read_text()) if art_path and Path(art_path).exists() else None)
    risky = [s for s in art.steps if s.risk is RiskClass.RISKY] if art else []
    checks.append(("distilled artifact carries exactly one RISKY CLICK step (approval required) whose postcondition targets the confirmation page",
                   len(risky) == 1 and risky[0].requires_approval and risky[0].action.value == "CLICK" and "confirmation" in json.dumps(risky[0].postcondition.model_dump(mode="json"))))
    run_id = (result or {}).get("run_id")
    ev = [json.loads(line) for line in (repo_root() / "runs" / run_id / "run.jsonl").read_text(encoding="utf-8").splitlines()] if run_id else []
    checks.append(("approval_used by operator logged", any(e["kind"] == "approval_used" and e.get("by") == "operator" for e in ev)))
    ok = all(v for _, v in checks)
    for name, v in checks:
        print(f"  [{'ok' if v else 'FAIL'}] {name}")
    print(f"\ndiscovery approval demo: {'PASS' if ok else 'FAIL'}   (run {run_id}; curate runs/{run_id} into evidence/escalation-demo-discovery/)")
    if not ok:
        print(err[-2000:])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
