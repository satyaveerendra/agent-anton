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
