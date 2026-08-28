"""SPEC §12: every run.jsonl line goes through the Redactor; the event envelope is inviolable."""

from __future__ import annotations

import json
from pathlib import Path

from anton.evidence.log import RunLog
from anton.policy.gate import PolicyConfig
from anton.policy.redact import Redactor
from anton.schema.snapshot import BBox, SemanticSnapshot, SnapshotElement


def test_events_are_scrubbed_and_the_envelope_kind_always_wins(policy: PolicyConfig, tmp_path: Path) -> None:
    r = Redactor(policy.redact_field_patterns, policy.redact_value_patterns, known_values={"demo-pass-1"})
    log = RunLog(tmp_path / "run", r)
    log.event("recovery", {"kind": "run_flow", "detector": "session_expired", "note": "typed demo-pass-1, ssn 900-37-2769"})
    snap = SemanticSnapshot(url="http://x/member/1", title="t", frame_count=2, elements=[
        SnapshotElement(ref="e1", role="cell", name="900-37-2769", text="900-37-2769", control_type="td", frame_path=["iframe[name=detail]"], bbox=BBox(x=0, y=0, w=1, h=1), enabled=True, visible=True)
    ])
    log.observation("click_search", snap)
    rel = log.write_json("snapshot-x.json", snap)
    log.close()
    lines = [json.loads(line) for line in (tmp_path / "run" / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in lines] == ["recovery", "observation"]
    assert lines[0]["data_kind"] == "run_flow" and lines[0]["seq"] == 1 and lines[1]["seq"] == 2
    assert lines[0]["note"] == "typed ***, ssn ***"
    obs = lines[1]
    assert obs["step_id"] == "click_search" and obs["url"] == "http://x/member/1" and obs["frame_count"] == 2
    assert obs["elements"][0]["text"] == "***" and obs["elements"][0]["frame_path"] == ["iframe[name=detail]"]
    assert "900-37-2769" not in (tmp_path / "run" / "run.jsonl").read_text(encoding="utf-8")
    assert "900-37-2769" not in rel.read_text(encoding="utf-8") and log.relative(rel) == "snapshot-x.json"


def test_verify_evidence_scans_text_lines_and_binary_bytes_but_never_env(tmp_path: Path) -> None:
    import json
    import shutil

    from anton.evidence.verify import needles, scan
    from anton.schema.loader import repo_root

    root = tmp_path
    (root / "target_app").mkdir()
    shutil.copy(repo_root() / "target_app" / "data.json", root / "target_app" / "data.json")
    n = needles(root / "target_app" / "data.json")
    ssn = next(v for k, v in n.items() if k.startswith("ssn:"))
    dob = next(v for k, v in n.items() if k.startswith("dob:"))
    (root / "evidence").mkdir()
    (root / "evidence" / "clean.jsonl").write_text(json.dumps({"text": "***"}) + "\n", encoding="utf-8")
    (root / "evidence" / "leak.jsonl").write_text("ok\n" + json.dumps({"text": ssn}) + "\n", encoding="utf-8")
    (root / "evidence" / "leak.tmp").write_text(f"dob {dob}\n", encoding="utf-8")  # no suffix allowlist: still scanned
    (root / "evidence" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + b"demo-pass-1" + b"\x00")  # binary: byte scan
    (root / "evidence" / ".env.local").write_text("ANTHROPIC_API_KEY=sk-ant-xyz\n", encoding="utf-8")  # never scanned
    hits = scan(root)
    assert ("evidence/leak.jsonl", 2, [k for k, v in n.items() if v == ssn][0]) in hits
    assert any(h[0] == "evidence/leak.tmp" and h[2].startswith("dob:") for h in hits)
    assert ("evidence/shot.png", 0, "demo-password") in hits
    assert not any(h[0].endswith(".env.local") for h in hits) and not any(h[0] == "evidence/clean.jsonl" for h in hits)
    assert len(n) == 52  # 25 SSNs + 25 DOBs + password + key prefix
