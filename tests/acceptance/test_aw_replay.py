"""SPEC §19 AW-1, AW-1b, AW-2, AW-3, AW-4, AW-7 — the real CLI against the real target app.

Every leg spawns `anton replay` as a subprocess (headless Chromium) with the fixture's exports
(`ANTON_TARGET_ORIGIN`, `ANTON_TARGET_DEBUG`, `ANTON_SECRET_DEMO_PASSWORD`) and reads the run
directory it created. Nothing in the verified path is mocked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from anton.schema.artifact import CapabilityArtifact, RiskClass
from anton.schema.loader import repo_root
from anton.schema.snapshot import SemanticSnapshot
from tests.acceptance.conftest import DATA, Target

pytestmark = pytest.mark.acceptance

#: SPEC §19: the artifacts under test are selected with ANTON_ARTIFACT_DIR (default artifacts/) so
#: phase 5 re-runs these tests unmodified against the distilled artifacts. Step ids are asserted
#: structurally from the artifact under test; the hand-authored ids appear only as comments.
ART_DIR = os.environ.get("ANTON_ARTIFACT_DIR", "artifacts")
BALANCE = f"{ART_DIR}/read_savings_balance.json"
SUBACCOUNT = f"{ART_DIR}/open_subaccount.json"
SUB_PARAMS = ["--param", "member_id=204817", "--param", "initial_deposit=50", "--param", "nickname=Holiday fund"]
RUNS = repo_root() / "runs"


class Run:
    def __init__(self, proc: subprocess.CompletedProcess[str]) -> None:
        self.proc = proc
        self.code = proc.returncode
        self.result = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else None
        self.run_dir = RUNS / self.result["run_id"] if self.result else None

    @property
    def steps(self) -> list[dict]:
        assert self.result is not None
        return self.result["steps"]

    def events(self) -> list[dict]:
        assert self.run_dir is not None
        return [json.loads(line) for line in (self.run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]

    def step(self, step_id: str) -> dict:
        return next(s for s in self.steps if s["step_id"] == step_id)


def replay(target: Target, artifact: str, *args: str, outputs_file: Path | None = None) -> Run:
    cmd = [sys.executable, "-m", "anton", "replay", artifact, "--url", target.origin, "--headless", *args]
    if outputs_file is not None:
        cmd += ["--outputs-file", str(outputs_file)]
    env = {**target.cli_env(), "ANTON_HEADLESS": "1"}
    proc = subprocess.run(cmd, cwd=repo_root(), env=env, capture_output=True, text=True, check=False, timeout=300)
    assert "Traceback" not in proc.stdout + proc.stderr, proc.stderr
    return Run(proc)


def seeded_savings_balance(member_id: str) -> Decimal:
    data = json.loads(DATA.read_text(encoding="utf-8"), parse_float=Decimal)
    member = next(m for m in data["members"] if str(m["member_id"]) == member_id)
    savings = next(a for a in member["accounts"] if a["type"] == "Savings")
    return Decimal(str(savings["balance"]))


def seeded_ssns() -> list[str]:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    return [m["ssn"] for m in data["members"]]


def artifact(path: str) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate_json((repo_root() / path).read_text(encoding="utf-8"))


def navigating_step_id(run: Run, path: str) -> str:
    """The declared step under which the first observation of ``path`` was logged."""
    obs = next(e for e in run.events() if e["kind"] == "observation" and urlsplit(e["url"]).path == path)
    return obs["step_id"]


def state(target: Target, member_id: str) -> dict:
    r = target.client().get(f"/__state/member/{member_id}")
    assert r.status == 200, r.body
    return json.loads(r.body)


# --- AW-1 --------------------------------------------------------------------------------------


def test_aw1_happy_path_replay(alpha: Target) -> None:
    run = replay(alpha, BALANCE, "--param", "member_id=204817")
    assert run.code == 0, run.proc.stderr
    r = run.result
    assert r["status"] == "SUCCESS" and r["failure"] is None and r["blocked"] is None
    assert Decimal(str(r["outputs"]["savings_balance"])) == seeded_savings_balance("204817")
    assert run.steps[0]["step_id"] == "__preconditions__" and run.steps[0]["status"] == "PASSED"
    spec = artifact(BALANCE)
    targeted = [st for st in spec.steps if st.target is not None]
    assert targeted
    for st in targeted:  # every step WITH A TARGET, from the artifact under test — not from the result
        s = run.step(st.id)
        assert s["resolution"] is not None and s["resolution"]["tier"] <= 2 and s["resolution"]["fingerprint_ok"], s
        assert s["drift_flag"] is False, s
    assert not any(s["drift_flag"] for s in run.steps)
    assert all(s["status"] == "PASSED" for s in run.steps)
    assert [s["step_id"] for s in run.steps] == ["__preconditions__", *[st.id for st in spec.steps], "__success__", "__outputs__"]
    assert run.run_dir is not None and (run.run_dir / "run.jsonl").exists()
    saved = json.loads((run.run_dir / "result.json").read_text(encoding="utf-8"))
    assert saved["redacted_view"] is True and saved["run_id"] == r["run_id"]
    assert r["redacted_view"] is True and r["tenant_id"] == "alpha" and r["overlay_id"] is None


# --- AW-1b ---------------------------------------------------------------------------------------


def test_aw1b_a_capability_not_a_macro(alpha: Target) -> None:
    run = replay(alpha, BALANCE, "--param", "member_id=310552")
    assert run.code == 0, run.proc.stderr
    assert run.result["status"] == "SUCCESS"
    assert Decimal(str(run.result["outputs"]["savings_balance"])) == seeded_savings_balance("310552")
    assert seeded_savings_balance("310552") != seeded_savings_balance("204817")  # the run really parameterized
    assert not any(s["drift_flag"] for s in run.steps)
    before = {p.name for p in RUNS.iterdir()} if RUNS.exists() else set()
    bad = replay(alpha, BALANCE, "--param", "member_id=abc")
    assert bad.code == 2 and bad.result is None and bad.proc.stderr.strip()  # one-line message, no result
    after = {p.name for p in RUNS.iterdir()} if RUNS.exists() else set()
    assert after == before


# --- AW-2 ------------------------------------------------------------------------------------------


def test_aw2_business_outcome_not_a_crash(alpha: Target) -> None:
    run = replay(alpha, BALANCE, "--param", "member_id=999999")
    assert run.code == 10, run.proc.stderr
    r = run.result
    assert r["status"] == "BUSINESS_OUTCOME" and r["outcome_code"] == "MEMBER_NOT_FOUND"
    assert r["outcome_detector_id"] == "member_not_found"
    last = run.steps[-1]  # click_search for the hand-authored artifact
    assert r["outcome_step_id"] == last["step_id"] and last["status"] == "BUSINESS_OUTCOME"
    assert "No member found" in r["outcome_detail"]
    assert r["failure"] is None and r["blocked"] is None and r["outputs"] is None
    events = run.events()
    action = next(e for e in events if e["kind"] == "action" and e["step_id"] == last["step_id"])
    detector = next(e for e in events if e["kind"] == "detector" and e["detector"] == "member_not_found")
    timeout = next(s for s in artifact(BALANCE).steps if s.id == last["step_id"]).wait_timeout_ms
    assert detector["seq"] > action["seq"] and detector["elapsed_ms"] <= timeout  # detectors short-circuit the wait
    assert "Traceback" not in run.proc.stdout + run.proc.stderr


# --- AW-3 -------------------------------------------------------------------------------------------


def test_aw3a_session_expiry_is_recovered_by_the_login_flow(target_factory) -> None:  # noqa: ANN001
    t = target_factory(tenant="alpha", inject=("expire",))
    run = replay(t, BALANCE, "--param", "member_id=204817")
    assert run.code == 0, run.proc.stderr
    assert run.result["status"] == "SUCCESS"
    with_recoveries = [s for s in run.steps if s["recoveries"]]
    assert len(with_recoveries) == 1 and with_recoveries[0]["recoveries"] == ["session_expired"], run.steps
    assert run.step("__preconditions__")["recoveries"] == []
    assert Decimal(str(run.result["outputs"]["savings_balance"])) == seeded_savings_balance("204817")


def test_aw3b_announcement_modal_is_dismissed(target_factory) -> None:  # noqa: ANN001
    t = target_factory(tenant="alpha", inject=("modal",))
    run = replay(t, BALANCE, "--param", "member_id=204817")
    assert run.code == 0, run.proc.stderr
    assert run.result["status"] == "SUCCESS"
    with_recoveries = [s for s in run.steps if s["recoveries"]]
    assert len(with_recoveries) == 1 and with_recoveries[0]["recoveries"] == ["announcement_modal"], run.steps


def test_aw3c_slow_page_is_absorbed_by_the_declarative_wait(target_factory) -> None:  # noqa: ANN001
    t = target_factory(tenant="alpha", inject=("slow",), slow_ms=5000)
    run = replay(t, BALANCE, "--param", "member_id=204817")
    assert run.code == 0, run.proc.stderr
    assert run.result["status"] == "SUCCESS"
    nav = run.step(navigating_step_id(run, "/member/204817"))  # the step that navigated to /member/<id> (click_search)
    assert nav["duration_ms"] >= 5000 and nav["attempts"] == 1 and nav["recoveries"] == [], nav


# --- AW-4 -------------------------------------------------------------------------------------------


def test_aw4_hard_failure_is_debuggable_and_redaction_holds(target_factory) -> None:  # noqa: ANN001
    t = target_factory(tenant="alpha", inject=("err500",))
    run = replay(t, SUBACCOUNT, *SUB_PARAMS)  # no consent flag: the failure precedes the risky step
    assert run.code == 20, run.proc.stderr
    r = run.result
    assert r["status"] == "FAILED" and r["failure"]["kind"] == "APP_ERROR"
    last = run.steps[-1]  # click_open_sub_account for the hand-authored artifact
    assert r["failure"]["step_id"] == last["step_id"] and last["status"] == "FAILED"
    step = next(s for s in artifact(SUBACCOUNT).steps if s.id == last["step_id"])
    assert r["failure"]["expected"] == step.postcondition.model_dump(mode="json")
    assert r["failure"]["observed_summary"]
    assert run.run_dir is not None
    evidence = r["failure"]["evidence"]
    assert evidence and all((run.run_dir / p).exists() for p in evidence), evidence
    pngs = [p for p in evidence if p.endswith(".png")]
    assert pngs and (run.run_dir / pngs[0]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not any(e["kind"] == "mask_unresolved" for e in run.events())  # masked, not failed-closed
    snaps = [p for p in evidence if p.endswith(".json")]
    assert snaps
    SemanticSnapshot.model_validate_json((run.run_dir / snaps[0]).read_text(encoding="utf-8"))
    # Redaction under failure: a pre-act observation of /member/<id> with a masked pane element ...
    observations = [e for e in run.events() if e["kind"] == "observation" and urlsplit(e["url"]).path == "/member/204817"]
    assert observations
    assert any(
        el["frame_path"] == ["iframe[name=detail]"] and el["text"] == "***"
        for e in observations for el in e["elements"]
    ), "no masked pane element in any /member/<id> observation"
    # ... and not one seeded SSN anywhere in the run tree.
    blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in run.run_dir.rglob("*") if p.is_file() and p.suffix != ".png")
    for ssn in seeded_ssns():
        assert ssn not in blob, f"SSN leaked into {run.run_dir}"
    for p in run.run_dir.rglob("*.png"):
        for ssn in seeded_ssns():
            assert ssn.encode() not in p.read_bytes()


# --- AW-7 -----------------------------------------------------------------------------------------------


def risky_step_id() -> str:
    steps = [s for s in artifact(SUBACCOUNT).steps if s.risk is RiskClass.RISKY]
    assert len(steps) == 1
    return steps[0].id


def test_aw7a_risky_step_blocks_without_consent(alpha: Target) -> None:
    before = state(alpha, "204817")
    run = replay(alpha, SUBACCOUNT, *SUB_PARAMS)
    assert run.code == 40, run.proc.stderr
    r = run.result
    assert r["status"] == "BLOCKED" and r["blocked"]["kind"] == "APPROVAL_REQUIRED" and r["blocked"]["step_id"] == risky_step_id()
    assert r["failure"] is None and r["outputs"] is None
    events = run.events()
    gate = next(e for e in events if e["kind"] == "gate" and e["step_id"] == risky_step_id() and e["decision"] == "NeedsApproval")
    assert not any(e["kind"] == "action" and e["seq"] > gate["seq"] for e in events)
    after = state(alpha, "204817")
    assert len(after["accounts"]) == len(before["accounts"]) and after["runtime_created"] == before["runtime_created"]


def test_aw7b_approved_risky_step_succeeds_and_output_stays_redacted(alpha: Target, tmp_path: Path) -> None:
    out = tmp_path / "outputs.json"
    run = replay(alpha, SUBACCOUNT, *SUB_PARAMS, "--approve-risky", outputs_file=out)
    assert run.code == 0, run.proc.stderr
    r = run.result
    assert r["status"] == "SUCCESS" and r["redacted_view"] is True
    assert r["outputs"]["account_number"] == "***"
    raw = json.loads(out.read_text(encoding="utf-8"))
    account_number = raw["account_number"]
    assert account_number and account_number != "***"
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    st = state(alpha, "204817")
    assert any(str(a.get("account_no")) == account_number for a in st["accounts"] + st["runtime_created"]), st
    assert account_number not in run.proc.stdout
    assert account_number not in (run.run_dir / "run.jsonl").read_text(encoding="utf-8")  # type: ignore[operator]
    approval = [e for e in run.events() if e["kind"] == "approval_used"]
    assert approval and approval[0]["by"] == "caller" and approval[0]["step_id"] == risky_step_id()


def test_aw7c_permission_denied_is_a_business_outcome_at_the_risky_step(target_factory) -> None:  # noqa: ANN001
    t = target_factory(tenant="alpha", inject=("deny",))
    run = replay(t, SUBACCOUNT, *SUB_PARAMS, "--approve-risky")
    assert run.code == 10, run.proc.stderr
    r = run.result
    assert r["status"] == "BUSINESS_OUTCOME" and r["outcome_code"] == "PERMISSION_DENIED" and r["outcome_step_id"] == risky_step_id()


def test_aw7d_validation_error_is_a_business_outcome(alpha: Target) -> None:
    run = replay(alpha, SUBACCOUNT, "--param", "member_id=204817", "--param", "initial_deposit=10", "--param", "nickname=Holiday fund", "--approve-risky")
    assert run.code == 10, run.proc.stderr
    r = run.result
    assert r["status"] == "BUSINESS_OUTCOME" and r["outcome_code"] == "VALIDATION_ERROR"
    assert r["outcome_step_id"] == run.steps[-1]["step_id"]  # click_continue_to_review for the hand-authored artifact
    assert "at least $25" in r["outcome_detail"], r["outcome_detail"]


def test_aw7e_draft_artifact_with_a_risky_step_is_refused_without_a_browser(alpha: Target, tmp_path: Path) -> None:
    data = json.loads((repo_root() / SUBACCOUNT).read_text(encoding="utf-8"))
    data["status"] = "draft"  # the stale approval block stays and is ignored
    draft = tmp_path / "open_subaccount_draft.json"
    draft.write_text(json.dumps(data, indent=2), encoding="utf-8")
    run = replay(alpha, str(draft), *SUB_PARAMS, "--allow-draft", "--approve-risky")
    assert run.code == 30, run.proc.stderr
    r = run.result
    assert r["status"] == "FAILED" and r["failure"]["kind"] == "POLICY_VIOLATION" and "DRAFT_RISKY" in r["failure"]["observed_summary"]
    assert run.run_dir is not None and (run.run_dir / "result.json").exists()
    assert not list(run.run_dir.rglob("*.png"))
