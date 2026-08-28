"""SPEC §16 "Escalation triggers" + "Lease": interventions with a fake surface, the lease table, 409s."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from anton.escalation.controller import TRANSITIONS, EscalationController, Lease, LeaseError
from anton.escalation.operator_app import create_app
from anton.evidence.log import RunLog
from anton.policy.gate import LEASE_TRANSITIONS, ActionGate, LeaseState, load_policy
from anton.policy.redact import Redactor
from anton.replay.engine import ReplayEngine, ReplayOptions, _Blocked, _Failed
from anton.replay.locate import resolve_candidate
from anton.schema.artifact import (
    ActionType,
    ElementFingerprint,
    LocatorLadder,
    RoleNameLocator,
    StepSpec,
    TextVisible,
    UrlMatches,
)
from anton.schema.loader import load_bundle
from anton.schema.result import InterventionRequest, StepResult, StepStatus
from anton.schema.snapshot import BBox, SemanticSnapshot, SnapshotElement
from anton.surface.base import ResolvedElement
from tests.conftest import ladder, make_artifact, sig

ORIGIN = "http://127.0.0.1:8000"


# --- lease -------------------------------------------------------------------------------------------


SPEC_EDGES = {  # SPEC §11, literally
    (LeaseState.AGENT_ACTIVE, LeaseState.PAUSED), (LeaseState.PAUSED, LeaseState.HUMAN_ACTIVE), (LeaseState.HUMAN_ACTIVE, LeaseState.RESUMING),
    (LeaseState.RESUMING, LeaseState.AGENT_ACTIVE), (LeaseState.RESUMING, LeaseState.PAUSED), (LeaseState.PAUSED, LeaseState.AGENT_ACTIVE),
    (LeaseState.PAUSED, LeaseState.ABANDONED), (LeaseState.HUMAN_ACTIVE, LeaseState.ABANDONED),
}


def test_lease_table_matches_spec_edges() -> None:
    edges = {(src, tgt) for name, (srcs, tgt) in TRANSITIONS.items() for src in srcs}
    assert edges == SPEC_EDGES == set(LEASE_TRANSITIONS)  # every §11 edge, nothing more, in both tables
    events: list[dict] = []
    lease = Lease(on_transition=events.append)
    assert lease.request("trigger", by="agent") is LeaseState.PAUSED and lease.owner == "none"
    with pytest.raises(LeaseError):
        lease.request("handback")
    assert lease.request("take") is LeaseState.HUMAN_ACTIVE and lease.owner == "human"
    with pytest.raises(LeaseError):
        lease.request("approve")
    assert lease.request("handback") is LeaseState.RESUMING
    assert lease.request("re-escalate", by="agent") is LeaseState.PAUSED
    assert lease.request("deny") is LeaseState.AGENT_ACTIVE and lease.owner == "agent"
    assert lease.request("trigger", by="agent") is LeaseState.PAUSED and lease.request("abort") is LeaseState.ABANDONED
    with pytest.raises(LeaseError):
        lease.request("take")
    assert [e["transition"] for e in events] == ["trigger", "take", "handback", "re-escalate", "deny", "trigger", "abort"]


def make_controller(tmp_path: Path, **kw) -> EscalationController:
    policy = load_policy(env={"ANTON_TARGET_ORIGIN": ORIGIN})
    redactor = Redactor(policy.redact_field_patterns, policy.redact_value_patterns)
    log = RunLog(tmp_path / "run", redactor)
    return EscalationController(tmp_path / "run", "run-x", redactor, log, console_port=0, timeout_s=kw.pop("timeout_s", 60), start_console=False, pump=lambda ms: time.sleep(ms / 1000), **kw)


def request(kind: str = "APPROVAL", step_id: str = "click_confirm_and_open_account") -> InterventionRequest:
    from datetime import UTC, datetime

    return InterventionRequest(request_id="run-x-i1", run_id="run-x", kind=kind, step_id=step_id, reason="r", screenshot="", page_signature="p", console_url="http://127.0.0.1:0", requested_at=datetime.now(UTC), lease_state="PAUSED")  # type: ignore[arg-type]


def test_console_api_transitions_and_409s(tmp_path: Path) -> None:
    ctl = make_controller(tmp_path)
    client = create_app(ctl).test_client()
    assert client.get("/api/state").get_json() == {"lease": "AGENT_ACTIVE", "owner": "agent", "pending": None}
    assert client.post("/api/take").status_code == 409  # nothing pending: AGENT_ACTIVE → HUMAN_ACTIVE is not an edge
    assert "<button" not in client.get("/").get_data(as_text=True)
    ctl.pending = request("TAKEOVER")
    ctl.log.write_json("intervention.json", ctl.pending)
    ctl.lease.request("trigger", by="agent")
    st = client.get("/api/state").get_json()
    assert st["lease"] == "PAUSED" and st["pending"]["kind"] == "TAKEOVER" and st["pending"]["step_id"] == "click_confirm_and_open_account"
    page = client.get("/").get_data(as_text=True)
    assert "<button" in page and "Take control" in page and "Approve once" not in page  # TAKEOVER offers no approve/deny
    assert client.post("/api/approve").status_code == 409 and client.post("/api/deny").status_code == 409  # APPROVAL only
    assert client.post("/api/handback").status_code == 409
    assert client.post("/api/take").get_json()["lease"] == "HUMAN_ACTIVE"
    ctl.pending = request("APPROVAL")
    assert client.post("/api/approve").status_code == 409 and client.post("/api/deny").status_code == 409  # APPROVAL pending, but not from HUMAN_ACTIVE
    assert ctl.approvals.pending() == []  # a refused approve grants nothing
    ctl.pending = request("TAKEOVER")
    assert client.post("/api/take").status_code == 409
    assert client.post("/api/handback").get_json()["lease"] == "RESUMING"
    assert client.post("/api/abort").status_code == 409  # only from PAUSED / HUMAN_ACTIVE
    ctl.lease.request("resume", by="agent")
    ctl.pending = request("APPROVAL")
    ctl.lease.request("trigger", by="agent")
    assert "Approve once" in client.get("/").get_data(as_text=True)
    r = client.post("/api/approve").get_json()
    assert r["lease"] == "AGENT_ACTIVE" and ctl.approvals.pending() == ["run-x-i1"]
    assert client.get("/screenshot").status_code == 404


def test_timeout_clock_resets_on_human_actions_and_expires_from_human_active(tmp_path: Path) -> None:
    ctl = make_controller(tmp_path, timeout_s=1)
    stop = {"at": time.monotonic() + 2.2}

    def human() -> None:
        time.sleep(0.2)
        ctl.lease.request("take")
        while time.monotonic() < stop["at"]:  # keep acting for ~2 s: each action resets the 1 s clock
            ctl.record_binding({"frame": None}, {"event": "click", "name": "x", "role": "button"})
            time.sleep(0.4)

    threading.Thread(target=human, daemon=True).start()
    t0 = time.monotonic()
    res, rec = ctl.escalate(request("TAKEOVER"))
    assert res == "timed_out" and rec.resolution == "timed_out" and time.monotonic() - t0 > 2.5
    assert rec.human_action_count >= 4 and rec.control_taken_at is not None and rec.handed_back_at is None
    last = ctl.lease.history[-1]
    assert (last["from"], last["to"], last["transition"]) == ("HUMAN_ACTIVE", "ABANDONED", "timeout") and ctl.recorder_armed is False
    # §11 order: PAUSED first, then intervention.json
    events = [json.loads(line) for line in (tmp_path / "run" / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["kind"] == "lease" and events[0]["to"] == "PAUSED"


def test_controller_wait_resolves_by_transition_and_by_timeout(tmp_path: Path) -> None:
    ctl = make_controller(tmp_path, timeout_s=1)
    req = request("TAKEOVER")
    # abandoned by timeout (no operator)
    res, rec = ctl.escalate(req)
    assert res == "timed_out" and rec.resolution == "timed_out" and ctl.lease.state is LeaseState.ABANDONED
    events = [json.loads(line) for line in (tmp_path / "run" / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["to"] for e in events if e["kind"] == "lease"] == ["PAUSED", "ABANDONED"]
    assert (tmp_path / "run" / "intervention.json").exists()
    # take → handback resolves "resumed" and counts human actions recorded while armed
    ctl2 = make_controller(tmp_path / "b", timeout_s=5)
    ctl2.record_binding({"frame": None}, {"event": "click", "name": "Confirm"})  # disarmed: dropped
    assert ctl2.human_action_count == 0
    ctl2.recorder_armed = True
    ctl2.record_binding({"frame": None}, {"event": "click", "name": "Hand back", "url": ctl2.console_url + "/"})  # a console click is never an app action
    assert ctl2.human_action_count == 0
    ctl2.recorder_armed = False

    def operator() -> None:
        time.sleep(0.3)
        ctl2.lease.request("take")
        time.sleep(0.3)
        ctl2.record_binding({"frame": None}, {"event": "click", "name": "Confirm and open account", "role": "button", "value": None, "input_type": None})
        ctl2.record_binding({"frame": None}, {"event": "change", "name": "Password", "role": "textbox", "value": "hunter2", "input_type": "text", "label_context": "Password"})
        ctl2.lease.request("handback")

    threading.Thread(target=operator, daemon=True).start()
    res, rec = ctl2.escalate(request("APPROVAL"))
    assert res == "resumed" and rec.resolution == "resumed" and rec.human_action_count == 2 and rec.control_taken_at is not None and rec.handed_back_at is not None
    lines = (tmp_path / "b" / "run" / "human_actions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["origin"] == "human" and json.loads(lines[1])["value"] == "***"  # masked by label pattern
    assert ctl2.recorder_armed is False


# --- engine triggers with a fake surface ------------------------------------------------------------------


def el(ref: str, role: str, text: str, x: float, y: float, w: float = 100, h: float = 20, *, name: str | None = None, ct: str = "td") -> SnapshotElement:
    return SnapshotElement(ref=ref, role=role, name=text if name is None else name, text=text, control_type=ct, frame_path=[], bbox=BBox(x=x, y=y, w=w, h=h), enabled=True, visible=True)


class ReviewSurface:
    """The review page: 'Confirm and open account' is present; clicking it never changes anything
    (so the postcondition never holds); 'Missing' never exists."""

    def __init__(self, tmp: Path, title: str = "Review") -> None:
        self.tmp, self.title, self.acts, self.gate_calls = tmp, title, [], 0
        self._snap = self.observe()

    def observe(self) -> SemanticSnapshot:
        self._snap = SemanticSnapshot(url=f"{ORIGIN}/member/204817/subaccount/review", title=self.title, frame_count=1, elements=[
            el("h", "text", "Review new sub-account", 10, 10, ct="span"), el("b", "button", "Confirm and open account", 10, 50, ct="button"), el("l", "link", "Sign out", 900, 5, ct="a")])
        return self._snap

    def last_snapshot(self) -> SemanticSnapshot:
        return self._snap

    def path(self) -> str:
        return "/member/204817/subaccount/review"

    def url(self) -> str:
        return ORIGIN + self.path()

    def viewport(self) -> tuple[int, int]:
        return (1280, 900)

    def resolve(self, candidate):  # noqa: ANN001, ANN201
        e = resolve_candidate(self._snap, candidate, self.viewport())
        return None if e is None else ResolvedElement(ref=e.ref, frame_path=e.frame_path, bbox=e.bbox, control_type=e.control_type, role=e.role, name=e.name, handle=None, text=e.text)

    def resolve_ref(self, ref: str):  # noqa: ANN201
        return None

    def act(self, action, timeout_ms: int) -> None:  # noqa: ANN001
        self.acts.append(action.action)

    def read(self, e: ResolvedElement, attr: str) -> str:
        return ""

    def screenshot(self, masks=None) -> Path:  # noqa: ANN001
        p = self.tmp / "screenshots" / f"shot-{len(self.acts)}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        return p


def make_engine(tmp_path: Path, workspace: dict[str, Path], step: StepSpec, *, escalate: bool, timeout_s: int = 5, surface_cls=ReviewSurface):  # noqa: ANN001, ANN202
    from datetime import UTC, datetime

    from anton.schema.artifact import Approval, content_hash

    art = make_artifact(app_profile=str(workspace["profile"]), steps=[step], success=sig(TextVisible(regex="never-there")), preconditions=sig(UrlMatches(regex="^/member/")))
    art = art.model_copy(update={"status": "approved", "approval": Approval(approved_at=datetime.now(UTC), approved_by="test", artifact_sha256=content_hash(art))})
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "art.json"
    path.write_text(art.model_dump_json(indent=2), encoding="utf-8")
    policy = load_policy(env={"ANTON_TARGET_ORIGIN": ORIGIN})
    bundle = load_bundle(path, policy=policy)
    redactor = Redactor(policy.redact_field_patterns, policy.redact_value_patterns)
    run_dir = tmp_path / "run"
    log = RunLog(run_dir, redactor)
    ctl = EscalationController(run_dir, "run-x", redactor, log, console_port=0, timeout_s=timeout_s, start_console=False, pump=lambda ms: time.sleep(ms / 1000)) if escalate else None
    surface = surface_cls(tmp_path)
    gate = ActionGate(policy)
    calls = {"n": 0}
    orig = gate.check

    def counting(action, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return orig(action, ctx)

    gate.check = counting  # type: ignore[method-assign]
    if ctl is not None:  # remember the gate-call count at the moment the run PARKS (SPEC §16: no further gate calls)
        orig_escalate = ctl.escalate

        def parking(req):  # noqa: ANN001, ANN202
            calls["at_park"] = calls["n"]
            return orig_escalate(req)

        ctl.escalate = parking  # type: ignore[method-assign]
    steps: list[StepResult] = []
    engine = ReplayEngine(bundle, {"member_id": "204817"}, ReplayOptions(allow_draft=True, escalate=escalate, sleep=lambda _t: None), surface, gate, log, redactor, steps, escalation=ctl)
    return engine, ctl, surface, steps, calls, run_dir


def confirm_step(**kw) -> StepSpec:
    base = dict(id="click_confirm_and_open_account", description="confirm", action=ActionType.CLICK, target=ladder("button", "Confirm and open account"), postcondition=sig(UrlMatches(regex="/confirmation$")), wait_timeout_ms=250, retries=0)
    base.update(kw)
    return StepSpec(**base)


def missing_step() -> StepSpec:
    return StepSpec(id="click_missing", description="x", action=ActionType.CLICK, target=ladder("button", "Missing"), postcondition=sig(UrlMatches(regex="/confirmation$")), wait_timeout_ms=250, retries=0)


def intervention(run_dir: Path) -> dict:
    return json.loads((run_dir / "intervention.json").read_text(encoding="utf-8"))


def test_needs_approval_without_consent_writes_an_approval_request_and_parks(tmp_path: Path, workspace: dict[str, Path]) -> None:
    engine, ctl, surface, steps, calls, run_dir = make_engine(tmp_path, workspace, confirm_step(), escalate=True, timeout_s=1)
    with pytest.raises(_Blocked) as e:
        engine._main_step(0)
    assert e.value.kind == "ESCALATION_UNRESOLVED" and e.value.intervention
    req = intervention(run_dir)
    assert req["kind"] == "APPROVAL" and req["step_id"] == "click_confirm_and_open_account" and req["target_fingerprint"]["name_snippet"] == "Confirm and open account"
    assert surface.acts == [] and calls["n"] == calls["at_park"] == 1  # parked: no further gate calls, no action
    assert engine.interventions[0].resolution == "timed_out" and ctl is not None and ctl.lease.state is LeaseState.ABANDONED


class MemberSurface(ReviewSurface):
    """A SAFE page (/member/<id>): a 'Reload' button whose click changes nothing — retries really happen."""

    def observe(self) -> SemanticSnapshot:
        self._snap = SemanticSnapshot(url=f"{ORIGIN}/member/204817", title=self.title, frame_count=1, elements=[
            el("h", "text", "Member Profile", 10, 10, ct="span"), el("b", "button", "Reload", 10, 50, ct="button"), el("l", "link", "Sign out", 900, 5, ct="a")])
        return self._snap

    def path(self) -> str:
        return "/member/204817"


def reload_step(retries: int = 1) -> StepSpec:
    return StepSpec(id="click_reload", description="reload", action=ActionType.CLICK, target=ladder("button", "Reload"), postcondition=sig(UrlMatches(regex="/confirmation$")), wait_timeout_ms=250, retries=retries)


@pytest.mark.parametrize("step, surface_cls, kind_expected, gate_calls, attempts", [
    (missing_step(), ReviewSurface, "LOCATOR_EXHAUSTED", 0, 1),   # the ladder never resolves: the gate is never reached
    (reload_step(retries=1), MemberSurface, "CHECKPOINT_MISS", 2, 2),  # SAFE step: one retry, then TAKEOVER
])
def test_failed_class_terminals_write_takeover_requests(tmp_path: Path, workspace: dict[str, Path], step: StepSpec, surface_cls, kind_expected: str, gate_calls: int, attempts: int) -> None:  # noqa: ANN001
    engine, ctl, surface, steps, calls, run_dir = make_engine(tmp_path, workspace, step, escalate=True, timeout_s=1, surface_cls=surface_cls)
    with pytest.raises(_Blocked) as e:
        engine._main_step(0)
    assert e.value.kind == "ESCALATION_UNRESOLVED"
    req = intervention(run_dir)
    assert req["kind"] == "TAKEOVER" and req["step_id"] == step.id and kind_expected in req["reason"]
    assert ctl is not None and ctl.lease.history[0]["to"] == "PAUSED"
    assert calls["at_park"] == gate_calls and calls["n"] == calls["at_park"]  # no gate call after the park
    assert steps[0].attempts == attempts and len(surface.acts) == gate_calls


def test_hard_failure_detector_escalates_as_takeover(tmp_path: Path, workspace: dict[str, Path]) -> None:
    class ErrorSurface(ReviewSurface):
        def __init__(self, tmp: Path) -> None:
            super().__init__(tmp, title="500 Internal Server Error")

    engine, ctl, surface, steps, calls, run_dir = make_engine(tmp_path, workspace, confirm_step(risk="SAFE"), escalate=True, timeout_s=1, surface_cls=ErrorSurface)
    engine.opts.approve_risky = True
    with pytest.raises(_Blocked) as e:
        engine._main_step(0)
    assert e.value.kind == "ESCALATION_UNRESOLVED"
    assert intervention(run_dir)["kind"] == "TAKEOVER" and "APP_ERROR" in intervention(run_dir)["reason"]
    assert ctl is not None and ctl.lease.history[0]["to"] == "PAUSED" and ctl.lease.state is LeaseState.ABANDONED
    assert surface.acts == [ActionType.CLICK] and calls["at_park"] == 1 and calls["n"] == 1  # one gated act, then parked


def test_without_escalate_the_same_conditions_are_terminal(tmp_path: Path, workspace: dict[str, Path]) -> None:
    engine, ctl, surface, steps, calls, run_dir = make_engine(tmp_path, workspace, confirm_step(), escalate=False)
    with pytest.raises(_Blocked) as e:
        engine._main_step(0)
    assert e.value.kind == "APPROVAL_REQUIRED" and not (run_dir / "intervention.json").exists()
    engine2, _, _, _, _, run_dir2 = make_engine(tmp_path / "2", workspace, missing_step(), escalate=False)
    with pytest.raises(_Failed) as f:
        engine2._main_step(0)
    assert f.value.kind == "LOCATOR_EXHAUSTED"


def test_operator_approval_is_consumed_once_and_hand_back_resumes(tmp_path: Path, workspace: dict[str, Path]) -> None:
    class ConfirmingSurface(ReviewSurface):
        """After the confirm click the page becomes the confirmation page (postcondition + success hold)."""

        def __init__(self, tmp: Path) -> None:
            self.done = False
            super().__init__(tmp)

        def observe(self) -> SemanticSnapshot:
            if not self.done:
                return super().observe()
            self._snap = SemanticSnapshot(url=f"{ORIGIN}/member/204817/subaccount/confirmation", title="Done", frame_count=1, elements=[el("h", "text", "Sub-account opened", 10, 10, ct="span"), el("n", "text", "never-there", 10, 40, ct="span")])
            return self._snap

        def path(self) -> str:
            return "/member/204817/subaccount/confirmation" if self.done else super().path()

        def act(self, action, timeout_ms: int) -> None:  # noqa: ANN001
            super().act(action, timeout_ms)
            self.done = True

    # (a) approve → the gate consumes the one-shot token and the click executes
    engine, ctl, surface, steps, calls, run_dir = make_engine(tmp_path, workspace, confirm_step(), escalate=True, timeout_s=5, surface_cls=ConfirmingSurface)
    assert ctl is not None

    def approver() -> None:
        while not (run_dir / "intervention.json").exists():
            time.sleep(0.05)
        time.sleep(0.2)
        ctl.grant_approval()
        ctl.lease.request("approve")

    threading.Thread(target=approver, daemon=True).start()
    assert engine._main_step(0) == 1 and surface.acts == [ActionType.CLICK] and steps[0].status is StepStatus.PASSED
    assert engine.interventions[0].resolution == "approved" and ctl.approvals.pending() == []
    events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "approval_used" and e["by"] == "operator" for e in events)
    assert [e["to"] for e in events if e["kind"] == "lease"] == ["PAUSED", "AGENT_ACTIVE"]
    # (b) take → (human clicks: the page changes) → handback: the step is HUMAN_COMPLETED and resume lands in __outputs__
    engine, ctl, surface, steps, calls, run_dir = make_engine(tmp_path / "b", workspace, confirm_step(), escalate=True, timeout_s=5, surface_cls=ConfirmingSurface)
    assert ctl is not None

    def human() -> None:
        while not (run_dir / "intervention.json").exists():
            time.sleep(0.05)
        ctl.lease.request("take")
        surface.done = True  # the human clicked Confirm in the live window
        ctl.record_binding({"frame": None}, {"event": "click", "name": "Confirm and open account", "role": "button"})
        ctl.lease.request("handback")

    threading.Thread(target=human, daemon=True).start()
    assert engine._main_step(0) == 1  # == len(steps): success holds → extraction
    rec = engine.interventions[0]
    assert rec.resolution == "resumed" and rec.resumed_at_step == "__outputs__" and rec.human_action_count == 1 and steps[0].status is StepStatus.HUMAN_COMPLETED
    assert surface.acts == []  # the agent never acted: no action event after PAUSED
    events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["to"] for e in events if e["kind"] == "lease"] == ["PAUSED", "HUMAN_ACTIVE", "RESUMING", "AGENT_ACTIVE"]
    paused_seq = next(e["seq"] for e in events if e["kind"] == "lease" and e["to"] == "PAUSED")
    assert not any(e["kind"] == "action" and e["seq"] > paused_seq for e in events)
    # (c) deny → BLOCKED(APPROVAL_DENIED)
    engine, ctl, surface, steps, calls, run_dir = make_engine(tmp_path / "c", workspace, confirm_step(), escalate=True, timeout_s=5)
    assert ctl is not None

    def denier() -> None:
        while not (run_dir / "intervention.json").exists():
            time.sleep(0.05)
        ctl.lease.request("deny")

    threading.Thread(target=denier, daemon=True).start()
    with pytest.raises(_Blocked) as e:
        engine._main_step(0)
    assert e.value.kind == "APPROVAL_DENIED" and engine.interventions[0].resolution == "denied" and surface.acts == []


def test_hand_back_with_nothing_satisfied_re_escalates_once_then_fails(tmp_path: Path, workspace: dict[str, Path]) -> None:
    engine, ctl, surface, steps, calls, run_dir = make_engine(tmp_path, workspace, confirm_step(), escalate=True, timeout_s=5)
    assert ctl is not None

    def human() -> None:
        for _ in range(2):  # two hand-backs, the page never changes
            while ctl.lease.state is not LeaseState.PAUSED:
                time.sleep(0.05)
            ctl.lease.request("take")
            ctl.lease.request("handback")
            time.sleep(0.3)

    threading.Thread(target=human, daemon=True).start()
    with pytest.raises(_Failed) as f:
        engine._main_step(0)
    assert f.value.kind == "CHECKPOINT_MISS" and "hand-backs" in f.value.observed
    assert [r.resolution for r in engine.interventions] == ["resume_failed", "resumed"]
    events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["to"] for e in events if e["kind"] == "lease"] == ["PAUSED", "HUMAN_ACTIVE", "RESUMING", "PAUSED", "HUMAN_ACTIVE", "RESUMING", "AGENT_ACTIVE"]  # the run ends under agent ownership


def test_ladder_fingerprint_kept_for_rationale() -> None:
    lad = LocatorLadder(candidates=[RoleNameLocator(tier=1, role="button", name="x", rationale="why")], fingerprint=ElementFingerprint(control_type="button"))
    assert lad.candidates[0].rationale == "why"
