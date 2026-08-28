"""The AW-5b mechanics without a human (integration): the run parks on the RISKY confirm step, a
simulated operator takes control, a click is dispatched on the OWNING thread (inside a pump — the
same path a real click takes through the recorder binding), hands back, and the run resumes to
SUCCESS with the confirm step HUMAN_COMPLETED. AW-5b itself (`make verify-5b`) stays human."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import anton.escalation.controller as controller_mod
from anton.policy.gate import load_policy
from anton.replay.engine import ReplayOptions, run_replay
from anton.schema.loader import load_bundle
from anton.schema.result import ReplayStatus
from tests.acceptance.conftest import Target

pytestmark = pytest.mark.integration

ART_DIR = os.environ.get("ANTON_ARTIFACT_DIR", "artifacts")


def test_take_click_handback_resumes_to_success(target_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    target: Target = target_factory(tenant="alpha")
    monkeypatch.setenv("ANTON_SECRET_DEMO_PASSWORD", "demo-pass-1")
    monkeypatch.setenv("ANTON_HEADLESS", "1")
    bundle = load_bundle(f"{ART_DIR}/open_subaccount.json", policy=load_policy(env={"ANTON_TARGET_ORIGIN": target.origin}), url=target.origin)
    state: dict = {"page": None, "phase": "wait"}

    class SimulatedOperator(controller_mod.EscalationController):
        """Every pump while parked: take control, click Confirm in the live page, hand back."""

        @property
        def pump(self):  # noqa: ANN201
            return self._pump

        @pump.setter
        def pump(self, fn) -> None:  # noqa: ANN001
            def pumping(ms: int) -> None:
                fn(ms)
                if state["phase"] == "wait" and self.pending is not None and self.lease.state.value == "PAUSED":
                    state["phase"] = "taken"
                    self.lease.request("take")
                elif state["phase"] == "taken" and self.lease.state.value == "HUMAN_ACTIVE":
                    state["phase"] = "clicked"
                    page = state["page"]
                    page.get_by_role("button", name="Confirm and open account").click()  # the "human" click → recorder binding
                    fn(600)  # let the navigation and the binding callback land
                    self.lease.request("handback")

            self._pump = pumping

    monkeypatch.setattr(controller_mod, "EscalationController", SimulatedOperator)
    real_launch = __import__("anton.replay.engine", fromlist=["_launch"])._launch

    def launch_and_capture(opts, run_dir, record_binding=None):  # noqa: ANN001, ANN202
        pw, browser, context = real_launch(opts, run_dir, record_binding)
        orig_new_page = context.new_page

        def new_page():  # noqa: ANN202
            state["page"] = orig_new_page()
            return state["page"]

        context.new_page = new_page
        return pw, browser, context

    monkeypatch.setattr("anton.replay.engine._launch", launch_and_capture)
    outputs = tmp_path / "outputs.json"
    result = run_replay(bundle, {"member_id": "204817", "initial_deposit": "50", "nickname": "Holiday fund"},
                        ReplayOptions(escalate=True, escalate_timeout_s=60, console_port=0, run_dir=tmp_path / "run", outputs_file=outputs, headless=True))
    assert result.status is ReplayStatus.SUCCESS, result.model_dump()
    assert result.outputs == {"account_number": "***"}
    iv = result.interventions[0]
    assert iv.kind == "APPROVAL" and iv.resolution == "resumed" and iv.human_action_count >= 1 and iv.resumed_at_step == "__outputs__"
    risky = next(s for s in result.steps if s.step_id == "click_confirm_and_open_account")
    assert risky.status.value == "HUMAN_COMPLETED"
    ev = [json.loads(line) for line in (tmp_path / "run" / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(e["from"], e["to"]) for e in ev if e["kind"] == "lease"] == [("AGENT_ACTIVE", "PAUSED"), ("PAUSED", "HUMAN_ACTIVE"), ("HUMAN_ACTIVE", "RESUMING"), ("RESUMING", "AGENT_ACTIVE")]
    seqs = {e["to"]: e["seq"] for e in ev if e["kind"] == "lease"}
    assert not any(e["kind"] == "action" and seqs["HUMAN_ACTIVE"] < e["seq"] < seqs["RESUMING"] for e in ev)
    human = [json.loads(line) for line in (tmp_path / "run" / "human_actions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert human and human[0]["origin"] == "human" and human[0]["descriptor"]["name"] == "Confirm and open account" and human[0]["event"] == "click"
    st = json.loads(target.client().get("/__state/member/204817").body)
    assert len(st["runtime_created"]) == 1  # the human's click opened exactly one account
    assert json.loads(outputs.read_text(encoding="utf-8"))["account_number"] == str(st["runtime_created"][0]["account_no"])
