"""Discovery SPIKE (SPEC §17 phase 4): a minimal ``act`` / ``read`` / ``finish`` loop — no distiller,
no artifact. It exists to de-risk phase 5: can the model, given the redacted digest + masked
screenshot, reach the member pane and cite the balance cell's ref in ``finish``?

Everything the model does goes through the same seam as replay: the harness logs in THROUGH
THE REPLAY ENGINE (the model never sees credentials), every ``act`` passes ``ActionGate.check``,
every observation is the redacted ``SemanticSnapshot``, every screenshot is masked, and the run
directory carries ``turns.jsonl`` (one ``TurnRecord`` per turn) + ``transcript.jsonl`` (scrubbed
request/response payloads). Model id from ``ANTON_MODEL`` (default ``claude-sonnet-5``); adaptive
thinking; no ``budget_tokens``, no prefill (SPEC §2).

Run:  ANTHROPIC_API_KEY=… python -m anton.discovery.agent --url http://127.0.0.1:8000 --param member_id=204817
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anton.evidence.log import RunLog
from anton.policy.gate import ActionGate, Allow, Deny, NeedsApproval, resolve_secret
from anton.policy.redact import Redactor
from anton.replay.engine import ReplayEngine, ReplayOptions, _launch, new_run_id
from anton.schema.artifact import ActionType, LiteralValue, ParamRef, RiskClass, SecretRef, ValueRef
from anton.schema.loader import LoadedBundle, load_bundle, repo_root
from anton.schema.result import StepResult
from anton.schema.snapshot import ElementDescriptor, SemanticSnapshot, SnapshotElement, TurnRecord
from anton.surface.web import PlaywrightSurface

DEFAULT_MODEL = "claude-sonnet-5"
MAX_STEPS = 30
WALL_CLOCK_S = 300
DIGEST_LIMIT = 60

TOOLS: list[dict[str, Any]] = [
    {
        "name": "act",
        "description": "Perform ONE UI action on an element of the current page digest. CLICK/FILL/SELECT/PRESS need a ref; NAVIGATE (value = URL) and a global PRESS (value = key name) use ref = null. FILL/SELECT values may be a literal, \"$param:<name>\" for a declared param, or \"$secret:<name>\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": ["string", "null"], "description": "element ref from the digest, e.g. \"e12\"; null only for NAVIGATE / global PRESS"},
                "action": {"type": "string", "enum": ["CLICK", "FILL", "SELECT", "PRESS", "NAVIGATE"]},
                "value": {"type": ["string", "null"], "description": "text to type / option to select / key / URL"},
                "rationale": {"type": "string", "description": "one sentence: why this action moves toward the goal"},
            },
            "required": ["ref", "action", "rationale"],
        },
    },
    {
        "name": "read",
        "description": "Return the (redacted) text of one element of the current page digest.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "rationale": {"type": "string"}},
            "required": ["ref", "rationale"],
        },
    },
    {
        "name": "finish",
        "description": "Declare the goal reached. For EVERY declared output give the ref of the element whose text IS that value; the harness reads the value itself — never transcribe it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "outputs": {"type": "object", "additionalProperties": {"type": "string"}, "description": "{output name: ref}"},
                "rationale": {"type": "string"},
            },
            "required": ["outputs", "rationale"],
        },
    },
]


def digest_of(snap: SemanticSnapshot) -> list[dict[str, Any]]:
    """≤ ~60 visible elements: interactive first, then text-bearing static elements (SPEC §10.1)."""
    visible = [e for e in snap.elements if e.visible]
    interactive = [e for e in visible if e.role in ("button", "link", "textbox", "checkbox", "radio", "combobox")]
    static = [e for e in visible if e not in interactive and e.text.strip()]
    chosen = (interactive + static)[:DIGEST_LIMIT]
    out = []
    for e in chosen:
        d: dict[str, Any] = {"ref": e.ref, "role": e.role, "name": e.name, "text": e.text[:120]}
        if e.frame_path:
            d["frame"] = "/".join(e.frame_path)
        if e.value is not None:
            d["value"] = e.value
        if e.checked is not None:
            d["checked"] = e.checked
        out.append(d)
    return out


def page_signature(snap: SemanticSnapshot, path: str) -> str:
    landmark = next((e.text for e in snap.elements if e.visible and e.role in ("heading", "text") and e.text.strip()), "")
    tuples = [(e.ref, e.role, e.name, e.value, e.checked, e.enabled) for e in snap.elements]
    digest = hashlib.sha256(json.dumps(tuples, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"{path}|{landmark[:40]}|{digest}"


def descriptor(el: SnapshotElement) -> ElementDescriptor:
    return ElementDescriptor(role=el.role, name=el.name, text=el.text, control_type=el.control_type, frame_path=list(el.frame_path), bbox=el.bbox)


def value_ref(value: str | None) -> ValueRef | None:
    if value is None:
        return None
    if value.startswith("$param:"):
        return ParamRef(name=value[len("$param:"):])
    if value.startswith("$secret:"):
        return SecretRef(name=value[len("$secret:"):])
    return LiteralValue(value=value)


class Spike:
    def __init__(self, bundle: LoadedBundle, goal: str, params: dict[str, str], outputs: dict[str, str], run_dir: Path, model: str) -> None:
        self.bundle, self.goal, self.params, self.outputs_decl, self.run_dir, self.model = bundle, goal, params, outputs, run_dir, model
        policy = bundle.policy
        self.redactor = Redactor(policy.redact_field_patterns, policy.redact_value_patterns)
        self.log = RunLog(run_dir, self.redactor)
        self.gate = ActionGate(policy)
        self.turns_path = run_dir / "turns.jsonl"
        self.transcript_path = run_dir / "transcript.jsonl"
        (run_dir / "digests").mkdir(exist_ok=True)

    # --- helpers -------------------------------------------------------------------------------------

    def _write(self, path: Path, obj: Any) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.redactor.scrub(obj), default=str) + "\n")

    def _system(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in self.params.items())
        outs = "; ".join(f"{k}: {v}" for k, v in self.outputs_decl.items())
        return (
            "You are operating a legacy back-office web console through a strict harness. You are ALREADY "
            f"authenticated and standing on the entry page. GOAL: {self.goal}\n"
            f"Declared params (use exactly these values; you may type \"$param:<name>\" as a FILL value): {params}\n"
            f"Declared outputs you must cite in finish: {outs}\n"
            "Each turn you get a digest of the visible elements (refs like e12 are valid ONLY for that turn) and a "
            "masked screenshot. Call exactly one tool per turn. Use `read` when you need to confirm an element's text. "
            "When the value of every declared output is visible on screen, call `finish` citing the ref of the element "
            "whose text is that value (for a table, the cell in the right row and column). Never transcribe values. "
            "No human is available; if the goal is impossible, say so in text and stop."
        )

    def _settle(self) -> SemanticSnapshot:
        """After an action: wait (bounded) for a pending navigation / the pane to land."""
        deadline = time.monotonic() + 3.0
        snap = self.surface.observe()
        while time.monotonic() < deadline and (snap.frame_count == 0 or not snap.elements):
            time.sleep(0.25)
            snap = self.surface.observe()
        # one more tick so a late iframe pane is included
        time.sleep(0.5)
        return self.surface.observe()

    # --- the loop --------------------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        import anthropic  # the only place outside tests where the SDK is imported (SPEC §9 purity)

        client = anthropic.Anthropic()
        opts = ReplayOptions(headless=os.environ.get("ANTON_HEADLESS", "") == "1")
        pw, browser, context = _launch(opts, self.run_dir)
        started = time.monotonic()
        summary: dict[str, Any] = {"run_id": self.run_dir.name, "status": "GAVE_UP", "turns": 0, "reached_member_pane": False, "finish": None}
        try:
            page = context.new_page()
            self.surface = PlaywrightSurface(page, self.redactor, self.bundle.profile.sensitive_elements, self.bundle.route_prefix, screenshot_dir=self.run_dir / "screenshots", on_event=self.log.event)
            # Login THROUGH THE REPLAY ENGINE — no model turn, credentials never leave the engine.
            steps: list[StepResult] = []
            engine = ReplayEngine(self.bundle, self.params, opts, self.surface, self.gate, self.log, self.redactor, steps)
            engine._preconditions()
            self.log.event("discovery", {"phase": "authenticated", "url": self.surface.path(), "model": self.model})
            messages: list[dict[str, Any]] = []
            sig_history: list[str] = []
            last_act: tuple[str, str] | None = None
            repeat = 0
            for turn in range(1, MAX_STEPS + 1):
                if time.monotonic() - started > WALL_CLOCK_S:
                    self.log.event("discovery", {"phase": "wall_clock", "turn": turn})
                    break
                snap = self.surface.observe()
                if "/member/" in self.surface.path() and any(e.frame_path for e in snap.elements):
                    summary["reached_member_pane"] = True
                digest = digest_of(snap)
                digest_path = self.run_dir / "digests" / f"turn-{turn:02d}.json"
                digest_path.write_text(json.dumps(self.redactor.scrub({"url": self.surface.path(), "title": snap.title, "elements": digest}), indent=1) + "\n", encoding="utf-8")
                shot = self.surface.screenshot(self.bundle.profile.sensitive_elements)
                png_b64 = base64.standard_b64encode(shot.read_bytes()).decode("ascii")
                # keep the history short: older digests collapse to a stub (SPEC §10.1 "a short history")
                for m in messages:
                    if m["role"] == "user" and isinstance(m["content"], list):
                        m["content"] = [c for c in m["content"] if c.get("type") != "image"]
                        for c in m["content"]:
                            if c.get("type") == "text" and c["text"].startswith("PAGE DIGEST"):
                                c["text"] = "PAGE DIGEST (older turn, omitted)"
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": f"PAGE DIGEST turn {turn} — url path {self.surface.path()!r}, title {snap.title!r}:\n" + json.dumps(digest)},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
                ]})
                self._write(self.transcript_path, {"turn": turn, "request": {"model": self.model, "system": self._system(), "messages": [m if m["role"] == "assistant" else {"role": "user", "content": [c if c.get("type") != "image" else {"type": "image", "bytes": len(png_b64)} for c in (m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}])]} for m in messages]}})
                response = client.messages.create(
                    model=self.model, max_tokens=4000, system=self._system(), tools=TOOLS, messages=messages,
                    thinking={"type": "adaptive"},
                )
                content_dump = [b.model_dump() for b in response.content]
                self._write(self.transcript_path, {"turn": turn, "response": {"stop_reason": response.stop_reason, "content": content_dump, "usage": response.usage.model_dump()}})
                messages.append({"role": "assistant", "content": response.content})
                summary["turns"] = turn
                text = " ".join(b.text for b in response.content if b.type == "text").strip()
                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                if not tool_blocks:
                    self.log.event("discovery", {"phase": "stopped_without_tool", "turn": turn, "text": text[:300]})
                    break
                tool = tool_blocks[0]
                args = dict(tool.input)
                rationale = str(args.pop("rationale", "") or text)[:500]
                self.log.event("rationale", {"turn": turn, "tool": tool.name, "text": rationale})
                record = TurnRecord(turn=turn, digest_path=str(digest_path.relative_to(self.run_dir)), screenshot_path=self.log.relative(shot), rationale=rationale, tool=tool.name, args={}, gate_decision="n/a", page_signature_after="")
                result_text = ""
                done = False
                if tool.name == "read":
                    ref = str(args.get("ref"))
                    el = next((e for e in snap.elements if e.ref == ref), None)
                    record.args = {"ref": ref}
                    result_text = f"text of {ref}: {el.text!r}" if el is not None else f"unknown ref {ref}"
                    if el is not None:
                        record.executed = descriptor(el)
                elif tool.name == "act":
                    ref = args.get("ref")
                    action = ActionType(str(args.get("action")))
                    value = args.get("value")
                    record.args = {"ref": ref, "action": action.value, "value": value}
                    record.action = action
                    record.value_ref = value_ref(value)
                    el = next((e for e in snap.elements if e.ref == ref), None) if ref else None
                    target = self.surface.resolve_ref(str(ref)) if ref else None
                    if ref and target is None:
                        result_text = f"unknown or stale ref {ref}; re-read the digest"
                        record.gate_decision = "not_attempted"
                    else:
                        concrete_value, kind = None, None
                        if isinstance(record.value_ref, ParamRef):
                            concrete_value, kind = self.params.get(record.value_ref.name), "param"
                        elif isinstance(record.value_ref, SecretRef):
                            concrete_value, kind = resolve_secret(record.value_ref.name), "secret"
                            self.redactor.add_sensitive_value(concrete_value)
                        elif isinstance(record.value_ref, LiteralValue):
                            concrete_value, kind = record.value_ref.value, "literal"
                        from anton.surface.base import ConcreteAction

                        concrete = ConcreteAction(action, target=target, value=concrete_value, url=concrete_value if action is ActionType.NAVIGATE else None, value_kind=kind)  # type: ignore[arg-type]
                        decision = engine._gate(concrete, f"turn{turn}", RiskClass.SAFE)
                        record.gate_decision = decision.kind if not isinstance(decision, Deny) else f"Deny:{decision.reason.value}"
                        if isinstance(decision, Allow):
                            if el is not None:
                                record.executed = descriptor(el)
                            engine._log_action(f"turn{turn}", concrete)
                            try:
                                self.surface.act(concrete, 8000)
                                result_text = f"{action.value} executed"
                            except Exception as e:  # noqa: BLE001 - reported to the model as a tool result
                                result_text = f"{action.value} failed: {str(e)[:200]}"
                        elif isinstance(decision, NeedsApproval):
                            result_text = "denied: RISKY action and this run has no --approve-risky consent"
                        else:
                            result_text = f"denied by policy: {decision.reason.value}: {decision.detail}"
                    after = self._settle()
                    sig = page_signature(after, self.surface.path())
                    key = (action.value, json.dumps(record.executed.model_dump(mode="json"), sort_keys=True) if record.executed else str(ref))
                    if last_act == key and sig_history and sig_history[-1] == sig:
                        repeat += 1
                    else:
                        repeat = 0
                    last_act = key
                    sig_history.append(sig)
                    record.page_signature_after = sig
                    if repeat >= 2 or (len(sig_history) >= 6 and len(set(sig_history[-6:])) == 1):
                        self._write(self.turns_path, record.model_dump(mode="json"))
                        self.log.event("discovery", {"phase": "dead_end", "turn": turn})
                        break
                elif tool.name == "finish":
                    outputs = dict(args.get("outputs") or {})
                    record.args = {"outputs": outputs}
                    extracted: dict[str, Any] = {}
                    for name, ref in outputs.items():
                        el = next((e for e in snap.elements if e.ref == ref), None)
                        live = self.surface.resolve_ref(ref)
                        raw = self.surface.read(live, "text") if live is not None else None
                        extracted[name] = {"ref": ref, "descriptor": descriptor(el).model_dump(mode="json") if el else None, "value": raw}
                    summary["finish"] = extracted
                    summary["status"] = "SUCCESS" if all(v["value"] for v in extracted.values()) and set(extracted) == set(self.outputs_decl) else "FINISH_INCOMPLETE"
                    record.gate_decision = "n/a"
                    done = True
                else:
                    result_text = f"unknown tool {tool.name}"
                if not record.page_signature_after:
                    record.page_signature_after = page_signature(self.surface.observe(), self.surface.path())
                self._write(self.turns_path, record.model_dump(mode="json"))
                if done:
                    break
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool.id, "content": self.redactor.scrub_text(result_text)}]})
        finally:
            for closer in (context.close, browser.close, pw.stop):
                try:
                    closer()
                except Exception:  # noqa: BLE001, S110
                    pass
            self.log.event("discovery", {"phase": "end", "summary": {k: v for k, v in summary.items() if k != "finish"}})
            self.log.close()
        (self.run_dir / "spike_summary.json").write_text(json.dumps(self.redactor.scrub(summary), indent=2, default=str) + "\n", encoding="utf-8")
        return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m anton.discovery.agent", description="discovery SPIKE (act/read/finish only)")
    ap.add_argument("--url", default=None, help="target origin (policy allowed_origins must match; export ANTON_TARGET_ORIGIN)")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--goal", default="Look up the member whose id is given by the member_id param and find the current balance of their Savings account.")
    ap.add_argument("--output", action="append", default=["savings_balance=Current balance of the member's Savings account (NUMBER, dollars)"], metavar="NAME=DESCRIPTION")
    ap.add_argument("--bundle", default="artifacts/read_savings_balance.json", help="artifact whose profile/policy/entry URL the spike borrows (its steps are NOT used)")
    args = ap.parse_args(argv)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("discovery spike skipped: ANTHROPIC_API_KEY is not set (SPEC §17 phase 4)", file=sys.stderr)
        return 2
    params = dict(p.split("=", 1) for p in args.param)
    outputs = dict(o.split("=", 1) for o in args.output)
    bundle = load_bundle(args.bundle, url=args.url)
    run_id = new_run_id()
    run_dir = repo_root() / "runs" / run_id
    run_dir.mkdir(parents=True)
    model = os.environ.get("ANTON_MODEL", DEFAULT_MODEL)
    spike = Spike(bundle, args.goal, params, outputs, run_dir, model)
    print(f"discovery spike run {run_id} (model {model}) → {run_dir}", file=sys.stderr)
    summary = spike.run()
    print(json.dumps({**summary, "finished_at": datetime.now(UTC).isoformat()}, indent=2, default=str))
    return 0 if summary["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
