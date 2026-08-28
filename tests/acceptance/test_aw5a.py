"""SPEC §19 AW-5a — escalation parks, is observable through the console, and can be abandoned /
times out / is denied. Real CLI, real target app, real console (headless Chromium)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from anton.schema.artifact import CapabilityArtifact, RiskClass
from anton.schema.loader import repo_root
from tests.acceptance.conftest import Target, free_port

RUNS = repo_root() / "runs"


def run_dir_for(name: str) -> Path:
    """Under runs/ (gitignored) like every other run, so `make curate-evidence` can find it."""
    import secrets
    from datetime import UTC, datetime

    return RUNS / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-5a{name}{secrets.token_hex(2)}"

pytestmark = pytest.mark.acceptance

ART_DIR = os.environ.get("ANTON_ARTIFACT_DIR", "artifacts")
SUBACCOUNT = f"{ART_DIR}/open_subaccount.json"
SUB_PARAMS = ["--param", "member_id=204817", "--param", "initial_deposit=50", "--param", "nickname=Holiday fund"]


def risky_step_id() -> str:
    art = CapabilityArtifact.model_validate_json((repo_root() / SUBACCOUNT).read_text(encoding="utf-8"))
    steps = [s for s in art.steps if s.risk is RiskClass.RISKY]
    assert len(steps) == 1
    return steps[0].id


def api(port: int, method: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def start_parked_replay(target: Target, run_dir: Path, port: int, timeout_s: int, *extra: str) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "anton", "replay", SUBACCOUNT, *SUB_PARAMS, "--url", target.origin, "--escalate", "--escalate-timeout", str(timeout_s),
           "--console-port", str(port), "--run-dir", str(run_dir), "--headless", *extra]
    env = {**target.cli_env(), "ANTON_HEADLESS": "1"}
    return subprocess.Popen(cmd, cwd=repo_root(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(pred, timeout: float, what: str):  # noqa: ANN001, ANN201
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def events(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]


def finish(proc: subprocess.Popen, timeout: float = 120) -> tuple[int, dict, str]:
    out, err = proc.communicate(timeout=timeout)
    assert "Traceback" not in out + err, err
    return proc.returncode, json.loads(out), err


def test_aw5a_park_take_abort(alpha: Target, tmp_path: Path) -> None:
    port = free_port()
    run_dir = run_dir_for("abort")
    proc = start_parked_replay(alpha, run_dir, port, 60)
    try:
        def read_request():  # noqa: ANN202
            try:
                return json.loads((run_dir / "intervention.json").read_text(encoding="utf-8")) if (run_dir / "intervention.json").exists() else None
            except json.JSONDecodeError:
                return None

        req = wait_for(read_request, 30, "intervention.json")
        assert req["kind"] == "APPROVAL" and req["step_id"] == risky_step_id()
        code, st = wait_for(lambda: (lambda r: r if r[0] == 200 else None)(api_safe(port)), 10, "console")
        assert st["lease"] == "PAUSED" and st["pending"]["kind"] == "APPROVAL" and st["pending"]["step_id"] == risky_step_id()
        code, st = api(port, "POST", "/api/take")
        assert code == 200 and st["lease"] == "HUMAN_ACTIVE"
        code, st = api(port, "POST", "/api/abort")
        assert code == 200 and st["lease"] == "ABANDONED"
        rc, result, err = finish(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 40 and result["status"] == "BLOCKED" and result["blocked"]["kind"] == "ESCALATION_UNRESOLVED"
    assert result["blocked"]["step_id"] == risky_step_id() and result["blocked"]["intervention_request_path"] == "intervention.json"
    assert result["interventions"][0]["resolution"] == "abandoned" and result["interventions"][0]["kind"] == "APPROVAL"
    ev = events(run_dir)
    lease = [(e["from"], e["to"]) for e in ev if e["kind"] == "lease"]
    assert lease == [("AGENT_ACTIVE", "PAUSED"), ("PAUSED", "HUMAN_ACTIVE"), ("HUMAN_ACTIVE", "ABANDONED")]
    paused = next(e["seq"] for e in ev if e["kind"] == "lease" and e["to"] == "PAUSED")
    assert not any(e["kind"] == "action" and e["seq"] > paused for e in ev)
    assert "operator console" in err


def api_safe(port: int) -> tuple[int, dict]:
    try:
        return api(port, "GET", "/api/state")
    except (urllib.error.URLError, ConnectionError, OSError):
        return 0, {}


def test_aw5a_timeout_without_operator(alpha: Target, tmp_path: Path) -> None:
    port = free_port()
    run_dir = run_dir_for("timeout")
    proc = start_parked_replay(alpha, run_dir, port, 5)
    try:
        rc, result, err = finish(proc, timeout=90)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 40 and result["status"] == "BLOCKED" and result["blocked"]["kind"] == "ESCALATION_UNRESOLVED"
    assert result["interventions"][0]["resolution"] == "timed_out"
    lease = [(e["from"], e["to"]) for e in events(run_dir) if e["kind"] == "lease"]
    assert lease == [("AGENT_ACTIVE", "PAUSED"), ("PAUSED", "ABANDONED")]


def test_aw5a_deny(alpha: Target, tmp_path: Path) -> None:
    port = free_port()
    run_dir = run_dir_for("deny")
    proc = start_parked_replay(alpha, run_dir, port, 60)
    try:
        wait_for(lambda: (run_dir / "intervention.json").exists(), 30, "intervention.json")
        wait_for(lambda: api_safe(port)[0] == 200, 10, "console")
        code, st = api(port, "POST", "/api/deny")
        assert code == 200 and st["lease"] == "AGENT_ACTIVE"
        rc, result, err = finish(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 40 and result["blocked"]["kind"] == "APPROVAL_DENIED" and result["interventions"][0]["resolution"] == "denied"
    lease = [(e["from"], e["to"]) for e in events(run_dir) if e["kind"] == "lease"]
    assert lease == [("AGENT_ACTIVE", "PAUSED"), ("PAUSED", "AGENT_ACTIVE")]
    st = alpha.client().get("/__state/member/204817")
    assert json.loads(st.body)["runtime_created"] == []  # nothing was opened
