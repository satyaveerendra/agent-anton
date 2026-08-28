"""SPEC §19 AW-6 — cross-tenant overlay: the alpha-recorded artifact fails on beta without the
overlay (beta genuinely differs) and passes with it (retargeted field, surviving row_cell/masks)."""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal

import pytest

from anton.schema.artifact import CapabilityArtifact
from anton.schema.loader import repo_root
from tests.acceptance.conftest import Target
from tests.acceptance.test_aw_replay import RUNS, Run, seeded_savings_balance

pytestmark = [pytest.mark.acceptance, pytest.mark.skipif(
    any(line.startswith("- P7-CUT ·") for line in (repo_root() / "docs" / "decisions.md").read_text(encoding="utf-8").splitlines()) or os.environ.get("ANTON_PHASE7_CUT") == "1",
    reason="phase 7 CUT (SPEC §19): '- P7-CUT ·' line in docs/decisions.md or ANTON_PHASE7_CUT=1")]

ART_DIR = os.environ.get("ANTON_ARTIFACT_DIR", "artifacts")
BALANCE = f"{ART_DIR}/read_savings_balance.json"
OVERLAY = "overlays/beta.yaml"


def replay(target: Target, *args: str) -> Run:
    cmd = [sys.executable, "-m", "anton", "replay", BALANCE, "--param", "member_id=204817", "--url", target.origin, "--headless", *args]
    env = {**target.cli_env(), "ANTON_HEADLESS": "1"}
    proc = subprocess.run(cmd, cwd=repo_root(), env=env, capture_output=True, text=True, check=False, timeout=300)
    assert "Traceback" not in proc.stdout + proc.stderr, proc.stderr
    return Run(proc)


def test_aw6_without_overlay_beta_genuinely_differs(beta: Target) -> None:
    run = replay(beta)  # --url origin-only: the alpha entry path /search 404s on beta
    assert run.code == 20, run.proc.stderr
    r = run.result
    assert r["status"] == "FAILED" and r["failure"]["kind"] == "CHECKPOINT_MISS" and r["failure"]["step_id"] == "__preconditions__"
    assert r["tenant_id"] == "alpha" and r["overlay_id"] is None


def test_aw6_with_overlay_the_alpha_artifact_passes_on_beta(beta: Target) -> None:
    run = replay(beta, "--overlay", OVERLAY)
    assert run.code == 0, run.proc.stderr
    r = run.result
    assert r["status"] == "SUCCESS" and r["tenant_id"] == "beta" and r["overlay_id"] == "beta"
    assert Decimal(str(r["outputs"]["savings_balance"])) == seeded_savings_balance("204817")  # same value: row_cell survived the column shift
    spec = CapabilityArtifact.model_validate_json((repo_root() / BALANCE).read_text(encoding="utf-8"))
    for st in spec.steps:
        if st.target is not None:
            s = run.step(st.id)
            # SPEC asks tier <= 2 and no drift; fingerprint_ok is asserted on top (intentionally stricter)
            assert s["resolution"] is not None and s["resolution"]["tier"] <= 2 and s["resolution"]["fingerprint_ok"], s
    assert not any(s["drift_flag"] for s in run.steps)
    assert any(s["resolution"] and s["resolution"]["source"] == "overlay" for s in run.steps), run.steps
    events = run.events()
    assert not any(e["kind"] == "mask_unresolved" for e in events)  # the SSN mask resolved on beta ...
    shots = [e for e in events if e["kind"] == "screenshot" and "/portal/member/204817" in e.get("url", "")]
    assert shots and shots[-1]["masks"].get("ssn_cell") is True and shots[-1]["masks"].get("dob_cell") is True, shots  # ... positively, on a member-page screenshot
    assert any(e["kind"] == "observation" and "/portal/member/204817" in e["url"] and any(el["frame_path"] == ["iframe[name=detail]"] and el["text"] == "***" for el in e["elements"]) for e in events)
    assert RUNS.exists()


def test_beta_complete_is_still_risky_through_the_url_rule(beta: Target) -> None:
    """SPEC §6: the overlay retargets the commit button to beta's 'Complete', and the URL-of-commit
    policy rule (§8) must still gate it — an overlay can retarget, never lower risk."""
    sub = f"{ART_DIR}/open_subaccount.json"
    args = [sub, "--param", "member_id=204817", "--param", "initial_deposit=50", "--param", "nickname=Holiday fund", "--url", beta.origin, "--overlay", "overlays/beta.open_subaccount.yaml", "--headless"]
    env = {**beta.cli_env(), "ANTON_HEADLESS": "1"}
    proc = subprocess.run([sys.executable, "-m", "anton", "replay", *args], cwd=repo_root(), env=env, capture_output=True, text=True, check=False, timeout=300)
    run = Run(proc)
    assert run.code == 40 and run.result["status"] == "BLOCKED" and run.result["blocked"]["kind"] == "APPROVAL_REQUIRED", proc.stderr
    gate = next(e for e in run.events() if e["kind"] == "gate" and e["decision"] == "NeedsApproval")
    assert gate["target"] == "Complete" and gate.get("rule") == 1  # beta's label, the URL rule
    assert run.step("click_confirm_and_open_account")["resolution"]["source"] == "overlay"
