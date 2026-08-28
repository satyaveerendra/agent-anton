"""SPEC §16 "Discovery loop (fake client)": dead-end counters, READ never advances, ask_human → give_up,
BLOCKED on a RISKY action without consent, and finish → distilled draft that loads."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anton.discovery.agent import DiscoverOptions, Discovery, discovery_bundle
from anton.policy.gate import load_policy
from anton.schema.artifact import ActionType, ParamSpec, ParamType
from anton.schema.loader import load_bundle
from anton.schema.result import exit_code
from anton.schema.snapshot import BBox, SemanticSnapshot, SnapshotElement, TurnRecord
from anton.schema.spec import CapabilitySpec, OutputDecl
from anton.surface.base import ResolvedElement
from tests.conftest import make_profile
from tests.unit.test_distill import member_page, search_page

ORIGIN = "http://127.0.0.1:8000"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def block(name: str, **inp: Any) -> Any:
    return SimpleNamespace(type="tool_use", id=f"t-{name}-{id(inp)}", name=name, input={"rationale": "because", **inp})


class FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.script, self.calls = list(script), 0
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw: Any) -> Any:
        self.calls += 1
        item = self.script.pop(0) if self.script else block("give_up", reason="script exhausted")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="thinking aloud"), item], stop_reason="tool_use", usage=None)


class FakeSurface:
    """Two pages: /search (form) and /member/204817 (pane table). CLICK on the Search button navigates."""

    def __init__(self, tmp: Path) -> None:
        self.page, self.value, self.tmp, self.acts = "search", "", tmp, []
        self._snap = self.observe()

    def _current(self) -> SemanticSnapshot:
        if self.page == "search":
            return search_page(self.value)
        if self.page == "review":
            return SemanticSnapshot(url=f"{ORIGIN}/member/204817/subaccount/review", title="Review", frame_count=1, elements=[
                SnapshotElement(ref="b9", role="button", name="Confirm and open account", text="Confirm and open account", control_type="button", frame_path=[], bbox=BBox(x=10, y=10, w=100, h=20), enabled=True, visible=True)])
        return member_page()

    def observe(self) -> SemanticSnapshot:
        self._snap = self._current()
        return self._snap

    def last_snapshot(self) -> SemanticSnapshot:
        return self._snap

    def path(self) -> str:
        return {"search": "/search", "member": "/member/204817", "review": "/member/204817/subaccount/review"}[self.page]

    def url(self) -> str:
        return ORIGIN + self.path()

    def viewport(self) -> tuple[int, int]:
        return (1280, 900)

    def resolve_ref(self, ref: str) -> ResolvedElement | None:
        el = next((e for e in self._snap.elements if e.ref == ref), None)
        if el is None:
            return None
        return ResolvedElement(ref=ref, frame_path=el.frame_path, bbox=el.bbox, control_type=el.control_type, role=el.role, name=el.name, handle=None, text=el.text)

    def resolve(self, candidate):  # noqa: ANN001, ANN201
        from anton.replay.locate import resolve_candidate

        el = resolve_candidate(self._snap, candidate, self.viewport())
        return self.resolve_ref(el.ref) if el else None

    def act(self, action, timeout_ms: int) -> None:  # noqa: ANN001
        self.acts.append((action.action, action.target.name if action.target else None, action.value))
        if action.action is ActionType.FILL and action.target and action.target.name == "Member ID":
            self.value = action.value or ""
        if action.action is ActionType.CLICK and action.target and action.target.name == "Search" and self.value == "204817":
            self.page = "member"

    def read(self, el: ResolvedElement, attr: str) -> str:
        e = next(x for x in self._snap.elements if x.ref == el.ref)
        return (e.value or "") if attr == "value" else e.text

    def screenshot(self, masks=None) -> Path:  # noqa: ANN001
        p = self.tmp / "screenshots" / f"shot-{len(self.acts)}-{id(masks) % 997}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG)
        return p


def spec(risky: bool = False) -> CapabilitySpec:
    return CapabilitySpec(capability_id="servicing.read_savings_balance", name="Read savings balance", description="d",
                          inputs=[ParamSpec(name="member_id", type=ParamType.STRING, description="id", pattern="^[0-9]+$")],
                          outputs=[OutputDecl(name="savings_balance", type="NUMBER", description="balance")])


def make(tmp_path: Path, script: list[Any], *, profile_yaml: Path | None = None, page: str = "search", approve_risky: bool = False, escalate: bool = False):  # noqa: ANN202
    policy = load_policy(env={"ANTON_TARGET_ORIGIN": ORIGIN})
    profile = make_profile()
    ppath = str(profile_yaml or "profiles/servicing.yaml")
    bundle = discovery_bundle(profile, policy, None, ppath)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    opts = DiscoverOptions(goal="balance", spec=spec(), params={"member_id": "204817"}, profile_path=ppath, out_dir=tmp_path / "out", approve_risky=approve_risky, escalate=escalate, model="fake", sleep=lambda _t: None)
    surface = FakeSurface(tmp_path)
    surface.page = page
    d = Discovery(opts, bundle, run_dir, client=FakeClient(script), surface_factory=lambda rd, r, log: (surface, lambda: None), login=False)
    return d, surface, run_dir


def turns_of(run_dir: Path) -> list[TurnRecord]:
    return [TurnRecord.model_validate_json(line) for line in (run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()]


def test_three_identical_actions_with_unchanged_page_is_a_dead_end(tmp_path: Path) -> None:
    d, surface, run_dir = make(tmp_path, [block("act", ref="b1", action="CLICK", value=None)] * 10)
    res = d.run()
    assert res.status == "GAVE_UP" and "3 identical" in (res.reason or "") and res.turns == 3 and exit_code(res) == 20
    assert len(surface.acts) == 3 and len(turns_of(run_dir)) == 3


def test_six_actions_without_page_change_is_a_dead_end(tmp_path: Path) -> None:
    alternating = [block("act", ref=r, action="CLICK", value=None) for r in ["b1", "c1", "c2", "b1", "c1", "c2", "b1", "c1"]]
    d, surface, run_dir = make(tmp_path, alternating)
    res = d.run()
    assert res.status == "GAVE_UP" and "6 actions" in (res.reason or "") and res.turns == 6


def test_read_turns_never_advance_the_counter(tmp_path: Path) -> None:
    script = []
    for _ in range(5):
        script += [block("act", ref="b1", action="CLICK", value=None), block("read", ref="h1")]
    d, surface, run_dir = make(tmp_path, script)
    res = d.run()
    assert res.status == "GAVE_UP" and "3 identical" in (res.reason or "") and res.turns == 5  # act,read,act,read,act
    assert [t.tool for t in turns_of(run_dir)] == ["act", "read", "act", "read", "act"]


def test_read_turns_do_not_increment_the_no_change_counter_either(tmp_path: Path) -> None:
    script = []
    for r in ["b1", "c1", "c2", "b1", "c1", "c2"]:
        script += [block("act", ref=r, action="CLICK", value=None), block("read", ref="h1")]
    d, surface, run_dir = make(tmp_path, script)
    res = d.run()
    assert res.status == "GAVE_UP" and "6 actions" in (res.reason or "") and res.turns == 11 and len(surface.acts) == 6
    assert [t.tool for t in turns_of(run_dir)] == ["act", "read"] * 5 + ["act"]


def test_identical_actions_that_change_the_page_are_not_a_dead_end(tmp_path: Path) -> None:
    script = [block("act", ref="i1", action="FILL", value=str(i)) for i in range(1, 8)]
    d, surface, run_dir = make(tmp_path, script)
    res = d.run()
    assert "dead-end" not in (res.reason or "") and res.turns == 8 and res.status == "GAVE_UP"  # script exhausted → give_up


def test_ask_human_without_escalate_maps_to_give_up(tmp_path: Path) -> None:
    d, _, run_dir = make(tmp_path, [block("ask_human", reason="stuck")])
    res = d.run()
    assert res.status == "GAVE_UP" and "ask_human" in (res.reason or "") and "give_up" in (res.reason or "") and exit_code(res) == 20
    assert turns_of(run_dir)[0].tool == "ask_human"


def test_risky_action_without_consent_is_blocked(tmp_path: Path) -> None:
    d, surface, run_dir = make(tmp_path, [block("act", ref="b9", action="CLICK", value=None)], page="review")
    res = d.run()
    assert res.status == "BLOCKED" and res.blocked is not None and res.blocked.kind == "APPROVAL_REQUIRED" and res.blocked.step_id == "turn1" and exit_code(res) == 40
    assert surface.acts == [] and turns_of(run_dir)[0].gate_decision == "NeedsApproval"
    events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "gate" and e["decision"] == "NeedsApproval" for e in events)
    d2, surface2, _ = make(tmp_path / "b", [block("act", ref="b9", action="CLICK", value=None), block("give_up", reason="done")], page="review", approve_risky=True) if (tmp_path / "b").mkdir() is None else (None, None, None)
    res2 = d2.run()
    assert surface2.acts and res2.status == "GAVE_UP"


def test_finish_distills_a_loadable_draft(tmp_path: Path, workspace: dict[str, Path]) -> None:
    script = [
        block("act", ref="i1", action="FILL", value="$param:member_id"),
        block("read", ref="i1"),
        block("act", ref="b1", action="CLICK", value=None),
        block("finish", outputs={"savings_balance": "s3"}),
    ]
    d, surface, run_dir = make(tmp_path, script, profile_yaml=workspace["profile"])
    res = d.run()
    assert res.status == "SUCCESS" and res.artifact_path and exit_code(res) == 0, res
    art_path = Path(res.artifact_path)
    assert art_path == tmp_path / "out" / "read_savings_balance.json"
    bundle = load_bundle(art_path, policy=load_policy(env={"ANTON_TARGET_ORIGIN": ORIGIN}))
    art = bundle.artifact
    assert art.status == "draft" and [s.id for s in art.steps] == ["fill_member_id", "click_search"] and art.provenance.discovery_run_id == run_dir.name
    assert surface.acts == [(ActionType.FILL, "Member ID", "204817"), (ActionType.CLICK, "Search", None)]
    recs = turns_of(run_dir)
    assert [r.tool for r in recs] == ["act", "read", "act", "finish"] and recs[0].value_ref is not None and recs[0].gate_decision == "Allow:SAFE"
    assert all((run_dir / r.digest_path).exists() and (run_dir / r.screenshot_path).exists() for r in recs)
    digest = json.loads((run_dir / recs[0].digest_path).read_text(encoding="utf-8"))
    assert digest["viewport"] == [1280, 900] and "snapshot" in digest
    events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for e in events if e["kind"] == "rationale") == 4 and (run_dir / "transcript.jsonl").exists() and (run_dir / "result.json").exists()


@pytest.mark.parametrize("kind", ["give_up"])
def test_give_up_is_recorded(tmp_path: Path, kind: str) -> None:
    d, _, run_dir = make(tmp_path, [block(kind, reason="cannot")])
    res = d.run()
    assert res.status == "GAVE_UP" and res.turns == 1 and res.artifact_path is None
