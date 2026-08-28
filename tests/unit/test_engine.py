"""Replay engine unit half (SPEC §9, §16): purity, params, outputs, resume_point, predicates, and
the start-up refusal that must never launch a browser (exit 30 pre-run; decisions.md P3-19)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from anton.policy.gate import load_policy
from anton.replay import engine as engine_mod
from anton.replay.engine import ParamError, ReplayOptions, coerce_output, run_replay, validate_params
from anton.replay.locate import resolve_candidate, resume_point
from anton.replay.predicates import Evaluator
from anton.schema.artifact import (
    ActionType,
    AllOf,
    BBoxLocator,
    Checked,
    ElementFingerprint,
    ElementVisible,
    ExtractionSpec,
    FieldValue,
    LiteralValue,
    LocatorLadder,
    LocatorTier,
    OutputSpec,
    OutputType,
    ParamRef,
    ParamSpec,
    ParamType,
    RiskClass,
    RoleNameLocator,
    SecretRef,
    StepSpec,
    TextVisible,
    TitleMatches,
    UrlMatches,
)
from anton.schema.loader import load_bundle
from anton.schema.result import FailureKind, ReplayStatus, exit_code
from anton.schema.snapshot import BBox, SemanticSnapshot, SnapshotElement
from anton.surface.base import ResolvedElement
from tests.conftest import ladder, make_artifact, make_steps, sig


def test_replay_package_never_imports_the_llm_client() -> None:
    """SPEC §9: a fresh interpreter imports the whole replay path and `anthropic` is absent."""
    code = (
        "import sys, anton.replay, anton.replay.engine, anton.replay.predicates, anton.replay.locate, anton.api, anton.cli;"
        "assert 'anthropic' not in sys.modules, 'anthropic leaked into the replay module graph';"
        "assert not [m for m in sys.modules if m.startswith('anton.discovery')], 'discovery leaked into replay'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_validate_params_enforces_the_paramspec() -> None:
    art = make_artifact(inputs=[
        ParamSpec(name="member_id", type=ParamType.STRING, description="id", pattern="^[0-9]+$"),
        ParamSpec(name="amount", type=ParamType.NUMBER, description="n", required=False),
        ParamSpec(name="kind", type=ParamType.ENUM, description="k", required=False, enum_values=["a", "b"]),
    ], steps=make_steps())
    assert validate_params(art, {"member_id": "204817"}) == {"member_id": "204817"}
    with pytest.raises(ParamError, match="pattern"):
        validate_params(art, {"member_id": "abc"})
    with pytest.raises(ParamError, match="missing required"):
        validate_params(art, {})
    with pytest.raises(ParamError, match="unknown"):
        validate_params(art, {"member_id": "1", "ghost": "x"})
    with pytest.raises(ParamError, match="NUMBER"):
        validate_params(art, {"member_id": "1", "amount": "ten"})
    with pytest.raises(ParamError, match="one of"):
        validate_params(art, {"member_id": "1", "kind": "z"})
    assert validate_params(art, {"member_id": "1", "amount": "12.5", "kind": "a"})["amount"] == "12.5"


def test_coerce_output_pipeline() -> None:
    loc = LocatorLadder(candidates=[RoleNameLocator(tier=1, role="cell", name="x", rationale="r")], fingerprint=ElementFingerprint(control_type="td"))
    money = OutputSpec(name="b", type=OutputType.NUMBER, description="d", extraction=ExtractionSpec(locator=loc, attr="text", normalize="money"))
    assert coerce_output(money, " $19,823.28 ") == 19823.28
    digits = OutputSpec(name="n", type=OutputType.INTEGER, description="d", extraction=ExtractionSpec(locator=loc, attr="text", normalize="digits"))
    assert coerce_output(digits, "Acct #7204817001") == 7204817001
    rx = OutputSpec(name="s", type=OutputType.STRING, description="d", extraction=ExtractionSpec(locator=loc, attr="text", post_process_regex=r"for (\d+)", normalize="trim"))
    assert coerce_output(rx, "No member found for 999999") == "999999"
    with pytest.raises(ValueError):
        coerce_output(rx, "nothing here")
    with pytest.raises(ValueError):
        coerce_output(money, "n/a")
    enum = OutputSpec(name="e", type=OutputType.ENUM, description="d", enum_values=["Savings"], extraction=ExtractionSpec(locator=loc, attr="text"))
    assert coerce_output(enum, " Savings ") == "Savings"
    with pytest.raises(ValueError):
        coerce_output(enum, "Checking")


def test_resume_point_picks_the_furthest_holding_signature() -> None:
    steps = make_steps()
    success = sig(UrlMatches(regex="^/done$"))
    holding: set[str] = set()

    def holds(s) -> bool:  # noqa: ANN001
        return any(p.model_dump_json() in holding for p in s.all_of) and all(p.model_dump_json() in holding for p in s.all_of)

    assert resume_point(steps, success, holds, start=0) is None
    holding = {p.model_dump_json() for p in steps[0].postcondition.all_of}
    assert resume_point(steps, success, holds, start=0) == 1
    holding |= {p.model_dump_json() for p in steps[1].postcondition.all_of}
    assert resume_point(steps, success, holds, start=0) == 2  # furthest wins, even with step 0 also holding
    assert resume_point(steps, success, holds, start=1) == 2
    holding |= {p.model_dump_json() for p in success.all_of}
    assert resume_point(steps, success, holds, start=0) == len(steps)  # success ⇒ straight to extraction


def el(ref: str, role: str, text: str, x: float, y: float, w: float = 100, h: float = 20, *, name: str | None = None,
       control_type: str = "td", value: str | None = None, checked: bool | None = None) -> SnapshotElement:
    return SnapshotElement(ref=ref, role=role, name=text if name is None else name, text=text, control_type=control_type,
                           frame_path=[], bbox=BBox(x=x, y=y, w=w, h=h), enabled=True, visible=True, value=value, checked=checked)


class FakeSurface:
    def __init__(self, snap: SemanticSnapshot, path: str = "/search", live: dict[str, dict[str, str]] | None = None) -> None:
        self.snap, self._path, self.live = snap, path, live or {}

    def observe(self) -> SemanticSnapshot:
        return self.snap

    def last_snapshot(self) -> SemanticSnapshot:
        return self.snap

    def path(self) -> str:
        return self._path

    def url(self) -> str:
        return "http://localhost:8000" + self._path

    def resolve(self, candidate):  # noqa: ANN001, ANN201
        e = resolve_candidate(self.snap, candidate, (1000, 800))
        if e is None:
            return None
        return ResolvedElement(ref=e.ref, frame_path=e.frame_path, bbox=e.bbox, control_type=e.control_type, role=e.role, name=e.name, handle=None, text=e.text)

    def read(self, el: ResolvedElement, attr: str) -> str:
        return self.live.get(el.ref, {}).get(attr, "")


def test_evaluator_covers_every_predicate_kind() -> None:
    snap = SemanticSnapshot(url="http://localhost:8000/search", title="Member Search — Console", frame_count=1, elements=[
        el("h", "text", "Member Search", 0, 0), el("l", "cell", "Member ID", 0, 30),
        el("i", "textbox", "", 110, 30, name="Member ID", control_type="input", value="***"),
        el("c", "checkbox", "", 110, 60, name="Terms", control_type="input", checked=True),
    ])
    live = {"i": {"value": "204817"}, "c": {"checked": "true"}}
    ev = Evaluator(FakeSurface(snap, live=live), {"member_id": "204817"})
    field = ladder("textbox", "Member ID", control_type="input", bbox=False)
    terms = ladder("checkbox", "Terms", control_type="input", bbox=False)
    assert ev.holds(UrlMatches(regex="^/search$"), snap) and not ev.holds(UrlMatches(regex="^/member/"), snap)
    assert ev.holds(TitleMatches(regex="Member Search"), snap)
    assert ev.holds(TextVisible(regex="Member $param:member_id|Member Search"), snap)
    assert ev.holds(ElementVisible(ladder=field), snap) and not ev.holds(ElementVisible(ladder=ladder("button", "Search", bbox=False)), snap)
    # FieldValue compares the LIVE value (the snapshot shows "***"), never the redacted snapshot.
    assert ev.holds(FieldValue(ladder=field, expected=ParamRef(name="member_id")), snap)
    assert ev.holds(FieldValue(ladder=field, expected=LiteralValue(value="204817")), snap)
    assert not ev.holds(FieldValue(ladder=field, expected=LiteralValue(value="1")), snap)
    assert ev.holds(FieldValue(ladder=field, expected=SecretRef(name="X")), snap)  # non-empty is all a secret asserts
    assert ev.holds(Checked(ladder=terms, expected=True), snap) and not ev.holds(Checked(ladder=terms, expected=False), snap)
    assert ev.holds(AllOf(predicates=[UrlMatches(regex="^/search$"), TextVisible(regex="Member Search")]), snap)
    assert not ev.holds(AllOf(predicates=[UrlMatches(regex="^/search$"), TextVisible(regex="nope")]), snap)
    assert ev.any_holds([TextVisible(regex="nope"), TextVisible(regex="Member")], snap)


def _draft_bundle_with_risky_step(workspace: dict[str, Path]):  # noqa: ANN202
    steps = make_steps()
    steps.append(StepSpec(
        id="click_confirm_and_open_account", description="confirm", action=ActionType.CLICK,
        target=ladder("button", "Confirm and open account"), risk=RiskClass.RISKY, retries=0,
        postcondition=sig(UrlMatches(regex="/confirmation$")),
    ))
    art = make_artifact(app_profile=str(workspace["profile"]), steps=steps, status="draft")
    path = workspace["dir"] / "draft_risky.json"
    path.write_text(art.model_dump_json(indent=2), encoding="utf-8")
    return load_bundle(path, policy=load_policy(env={"ANTON_TARGET_ORIGIN": "http://127.0.0.1:8000"}))


def test_draft_with_risky_step_is_refused_before_any_browser_launch(workspace: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC §8 start-up rules / AW-7e shape: exit 30, a run dir with result.json, no browser."""
    bundle = _draft_bundle_with_risky_step(workspace)

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a browser was launched for a refused draft")

    monkeypatch.setattr(engine_mod, "_launch", boom)
    t0 = time.monotonic()
    result = run_replay(bundle, {"member_id": "204817"}, ReplayOptions(allow_draft=True, approve_risky=True, runs_root=tmp_path / "runs"))
    assert time.monotonic() - t0 < 2
    assert result.status is ReplayStatus.FAILED and result.failure is not None
    assert result.failure.kind is FailureKind.POLICY_VIOLATION and "DRAFT_RISKY" in result.failure.observed_summary
    assert result.failure.step_id == "click_confirm_and_open_account" and result.failure.expected is None and result.failure.evidence == []
    assert exit_code(result) == 30 and result.steps == [] and result.redacted_view is True
    run_dir = tmp_path / "runs" / result.run_id
    assert (run_dir / "result.json").exists() and (run_dir / "run.jsonl").exists()
    assert not (run_dir / "screenshots").exists()
    saved = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "FAILED" and saved["failure"]["kind"] == "POLICY_VIOLATION"
    events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "gate" and e["reason"] == "DRAFT_RISKY" for e in events)
    assert not any(e["kind"] == "action" for e in events)


def test_cli_replay_usage_errors_exit_2_without_a_run_dir(workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from anton.cli import main
    from anton.schema.loader import repo_root

    runs = repo_root() / "runs"
    before = {p.name for p in runs.iterdir()} if runs.exists() else set()
    monkeypatch.setenv("ANTON_TARGET_ORIGIN", "http://127.0.0.1:8000")
    assert main(["replay", str(workspace["artifact"]), "--param", "member_id=abc"]) == 2
    assert "pattern" in capsys.readouterr().err
    assert main(["replay", str(workspace["artifact"]), "--param", "member_id"]) == 2
    assert main(["replay", str(workspace["artifact"]), "--param", "member_id=1", "--escalate"]) == 2
    assert "phase 6" in capsys.readouterr().err
    assert main(["replay", str(workspace["dir"] / "missing.json"), "--param", "member_id=1"]) == 2
    after = {p.name for p in runs.iterdir()} if runs.exists() else set()
    assert after == before  # SPEC §5: usage errors create no run directory


def test_bbox_candidate_survives_in_a_ladder_walk() -> None:
    """Tier 3 in the hand-authored artifacts is real: a bbox resolves and sets drift_flag."""
    from anton.replay.locate import walk_ladder

    snap = SemanticSnapshot(url="u", title="t", frame_count=1, elements=[el("b", "button", "Search", 100, 100, w=80, h=30, control_type="button")])
    ladder_ = LocatorLadder(candidates=[BBoxLocator(tier=LocatorTier.VISUAL, x=0.1, y=0.125, w=0.08, h=0.0375, rationale="r")], fingerprint=ElementFingerprint(role="button", control_type="button"))
    res = walk_ladder(FakeSurface(snap), ladder_, {})
    assert res is not None and res.drift_flag and res.resolution.tier is LocatorTier.VISUAL


def test_outputs_file_path_rules_and_mode(tmp_path: Path) -> None:
    from anton.replay.engine import check_outputs_path, write_outputs_file
    from anton.schema.loader import repo_root

    for bad in (repo_root() / "runs" / "x.json", repo_root() / "evidence" / "x.json", repo_root() / "runs"):
        with pytest.raises(ParamError, match="must not be placed under"):
            check_outputs_path(bad)
    with pytest.raises(ParamError, match="does not exist"):
        check_outputs_path(tmp_path / "missing" / "o.json")
    assert check_outputs_path(tmp_path / "o.json") == (tmp_path / "o.json").resolve()
    existing = tmp_path / "pre.json"
    existing.write_text("{}", encoding="utf-8")
    existing.chmod(0o644)
    write_outputs_file(existing, {"account_number": "7204817001"})
    assert oct(existing.stat().st_mode & 0o777) == "0o600"  # enforced even on a pre-existing file
    assert json.loads(existing.read_text(encoding="utf-8")) == {"account_number": "7204817001"}


def test_sensitive_param_values_never_appear_in_param_errors() -> None:
    art = make_artifact(inputs=[ParamSpec(name="member_id", type=ParamType.STRING, description="id", pattern="^[0-9]+$", sensitive=True)], steps=make_steps())
    with pytest.raises(ParamError) as e:
        validate_params(art, {"member_id": "secret-ish"})
    assert "secret-ish" not in str(e.value) and "***" in str(e.value)


def test_invoke_refuses_record_video_when_masks_are_declared_and_bad_outputs_paths(workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from anton.api import invoke
    from anton.schema.loader import repo_root

    monkeypatch.setenv("ANTON_TARGET_ORIGIN", "http://127.0.0.1:8000")
    with pytest.raises(ParamError, match="record-video"):
        invoke(workspace["artifact"], {"member_id": "1"}, record_video=True)
    with pytest.raises(ParamError, match="must not be placed under"):
        invoke(workspace["artifact"], {"member_id": "1"}, outputs_file=repo_root() / "runs" / "o.json")


def test_validate_params_requires_every_referenced_param() -> None:
    """A `$param:` token or ParamRef to an omitted OPTIONAL param would KeyError mid-run; refuse up front."""
    steps = make_steps()
    steps[1] = steps[1].model_copy(update={"postcondition": sig(UrlMatches(regex="^/member/$param:suffix$"))})
    art = make_artifact(inputs=[
        ParamSpec(name="member_id", type=ParamType.STRING, description="id", pattern="^[0-9]+$"),
        ParamSpec(name="suffix", type=ParamType.STRING, description="s", required=False),
    ], steps=steps)
    with pytest.raises(ParamError, match="referenced by the artifact"):
        validate_params(art, {"member_id": "1"})
    assert validate_params(art, {"member_id": "1", "suffix": "x"}) == {"member_id": "1", "suffix": "x"}
