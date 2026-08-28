"""``anton inspect`` / ``anton approve`` through the real CLI (SPEC §13), exit codes per §5."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from anton.schema.artifact import CapabilityArtifact, content_hash
from anton.schema.loader import repo_root
from anton.schema.result import EXIT_USAGE


def run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ANTON_TARGET_ORIGIN": "http://127.0.0.1:8000", **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "anton", *args], cwd=repo_root(), env=env, capture_output=True, text=True)


def test_approve_then_inspect(workspace: dict[str, Path]) -> None:
    artifact = workspace["artifact"]
    r = run("approve", str(artifact), "--by", "tester")
    assert r.returncode == 0, r.stderr
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["status"] == "approved" and data["approval"]["approved_by"] == "tester"
    a = CapabilityArtifact.model_validate(data)
    assert a.approval is not None and a.approval.artifact_sha256 == content_hash(a)

    r2 = run("inspect", str(artifact))
    assert r2.returncode == 0, r2.stderr
    out = r2.stdout
    assert "servicing.read_savings_balance" in out
    assert "hash VALID" in out
    assert "MEMBER_NOT_FOUND" in out and "[app]" in out
    assert "fill_member_id" in out and "click_search" in out
    assert "row_cell 'Savings'" in out and "col='Balance'" in out
    assert "requires_flows: login" in out

    # Approving twice is idempotent.
    r3 = run("approve", str(artifact))
    assert r3.returncode == 0 and "already approved" in r3.stdout


def test_inspect_with_overlay_reports_sources(workspace: dict[str, Path]) -> None:
    r = run("inspect", str(workspace["artifact"]), "--overlay", str(workspace["overlay"]))
    assert r.returncode == 0, r.stderr
    assert "overlay=beta" in r.stdout and "target (overlay)" in r.stdout and "route_prefix=/portal" in r.stdout


def test_edited_after_approval_is_refused_with_exit_2(workspace: dict[str, Path]) -> None:
    artifact = workspace["artifact"]
    assert run("approve", str(artifact)).returncode == 0
    data = json.loads(artifact.read_text(encoding="utf-8"))
    data["description"] = "edited after approval"
    artifact.write_text(json.dumps(data), encoding="utf-8")
    r = run("inspect", str(artifact))
    assert r.returncode == EXIT_USAGE
    assert "approval hash mismatch" in r.stderr
    assert r.stdout == ""


def test_loader_refusal_is_one_line_exit_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    r = run("inspect", str(bad))
    assert r.returncode == EXIT_USAGE
    assert r.stderr.strip().count("\n") == 0 and "invalid JSON" in r.stderr


def test_not_yet_commands_exit_2_with_phase_message() -> None:
    r = run("replay", "artifacts/x.json", "--param", "member_id=1")
    assert r.returncode == EXIT_USAGE and "file not found" in r.stderr  # replay exists since phase 4; a missing file is a loader refusal
    r = run("discover", "--goal", "x")
    assert r.returncode == EXIT_USAGE and "phase 5" in r.stderr


def test_reapprove_after_edit_rebinds_the_hash(workspace: dict[str, Path]) -> None:
    artifact = workspace["artifact"]
    assert run("approve", str(artifact)).returncode == 0
    data = json.loads(artifact.read_text(encoding="utf-8"))
    data["description"] = "edited after approval"
    artifact.write_text(json.dumps(data), encoding="utf-8")
    r = run("approve", str(artifact), "--by", "reviewer")
    assert r.returncode == 0 and "stale" in r.stdout, r.stderr
    a = CapabilityArtifact.model_validate_json(artifact.read_text(encoding="utf-8"))
    assert a.approval is not None and a.approval.artifact_sha256 == content_hash(a) and a.approval.approved_by == "reviewer"
    assert run("inspect", str(artifact)).returncode == 0


def test_missing_policy_file_is_exit_2_one_line(workspace: dict[str, Path], tmp_path: Path) -> None:
    r = run("inspect", str(workspace["artifact"]), "--policy", str(tmp_path / "nope.yaml"))
    assert r.returncode == EXIT_USAGE and "policy" in r.stderr and "Traceback" not in r.stderr
