"""Session lease + escalation controller (SPEC §11).

``Lease`` is the lock-protected state machine (edges = ``LEASE_TRANSITIONS`` in ``policy/gate.py``);
every transition is logged to run.jsonl. ``EscalationController`` owns the pending
``InterventionRequest``, the one-shot ``ApprovalStore``, the recorder's armed flag and the
``--escalate-timeout`` clock, and runs the OWNING thread's park loop: ``page.wait_for_timeout(250)``
pumps (never ``time.sleep`` — the exposed binding would starve), applying the transitions the console
thread requested. The console thread only ever calls ``Lease.request`` and reads files.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anton.evidence.log import RunLog
from anton.policy.gate import LEASE_TRANSITIONS, ApprovalStore, ApprovalToken, LeaseState
from anton.policy.redact import Redactor
from anton.schema.result import InterventionRecord, InterventionRequest

__all__ = ["LeaseError", "Lease", "TRANSITIONS", "EscalationController", "Resolution"]

#: transition name → (allowed source states, target state); the edge list is SPEC §11's.
TRANSITIONS: dict[str, tuple[frozenset[LeaseState], LeaseState]] = {
    "trigger": (frozenset({LeaseState.AGENT_ACTIVE}), LeaseState.PAUSED),
    "take": (frozenset({LeaseState.PAUSED}), LeaseState.HUMAN_ACTIVE),
    "handback": (frozenset({LeaseState.HUMAN_ACTIVE}), LeaseState.RESUMING),
    "resume": (frozenset({LeaseState.RESUMING}), LeaseState.AGENT_ACTIVE),
    "re-escalate": (frozenset({LeaseState.RESUMING}), LeaseState.PAUSED),
    "approve": (frozenset({LeaseState.PAUSED}), LeaseState.AGENT_ACTIVE),
    "deny": (frozenset({LeaseState.PAUSED}), LeaseState.AGENT_ACTIVE),
    "abort": (frozenset({LeaseState.PAUSED, LeaseState.HUMAN_ACTIVE}), LeaseState.ABANDONED),
    "timeout": (frozenset({LeaseState.PAUSED, LeaseState.HUMAN_ACTIVE}), LeaseState.ABANDONED),
}
_OWNER = {LeaseState.AGENT_ACTIVE: "agent", LeaseState.PAUSED: "none", LeaseState.HUMAN_ACTIVE: "human", LeaseState.RESUMING: "none", LeaseState.ABANDONED: "none"}

Resolution = str  # "resumed" | "approved" | "denied" | "abandoned" | "timed_out"


class LeaseError(Exception):
    """An invalid transition (the console answers 409)."""


class Lease:
    def __init__(self, on_transition: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._lock = threading.Lock()
        self.state = LeaseState.AGENT_ACTIVE
        self.owner = "agent"
        self.history: list[dict[str, Any]] = []
        self._on_transition = on_transition

    def request(self, transition: str, by: str = "operator") -> LeaseState:
        with self._lock:
            if transition not in TRANSITIONS:
                raise LeaseError(f"unknown transition {transition!r}")
            allowed, target = TRANSITIONS[transition]
            if self.state not in allowed or (self.state, target) not in LEASE_TRANSITIONS:
                raise LeaseError(f"{transition!r} is not valid from {self.state.value}")
            src = self.state
            self.state, self.owner = target, _OWNER[target]
            event = {"from": src.value, "to": target.value, "transition": transition, "by": by, "owner": self.owner, "ts": datetime.now(UTC).isoformat()}
            self.history.append(event)
            if self._on_transition is not None:
                self._on_transition(event)  # inside the lock: callbacks observe transitions in order
        return target

    def unlogged(self, start: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.history[start:])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"lease": self.state.value, "owner": self.owner}


class EscalationController:
    """One per run. ``escalate(request)`` parks the owning thread until the operator (or the
    timeout) resolves the intervention and returns ``(resolution, record)``."""

    def __init__(self, run_dir: Path, run_id: str, redactor: Redactor, log: RunLog, *, console_port: int = 7071, timeout_s: int = 600,
                 capability_id: str | None = None, goal: str | None = None, pump: Callable[[int], None] | None = None,
                 on_take: Callable[[], None] | None = None, on_handback: Callable[[], None] | None = None, start_console: bool = True) -> None:
        self.run_dir, self.run_id, self.redactor, self.log = Path(run_dir), run_id, redactor, log
        self.console_port, self.timeout_s = console_port, timeout_s
        self.capability_id, self.goal = capability_id, goal
        self.pump = pump or (lambda ms: time.sleep(ms / 1000))
        self.on_take, self.on_handback = on_take, on_handback
        self._start_console = start_console
        self.lease = Lease(on_transition=self._log_transition)
        self.approvals = ApprovalStore()
        self.pending: InterventionRequest | None = None
        self.recorder_armed = False
        self.human_action_count = 0
        self.human_actions_path = self.run_dir / "human_actions.jsonl"
        self._last_activity = time.monotonic()
        self._console_thread: threading.Thread | None = None
        self._server: Any | None = None
        self._seq = 0
        self.records: list[InterventionRecord] = []
        self._logged = 0  # lease.history entries already written to run.jsonl (owning thread only)
        #: Seconds spent parked — discovery's wall clock excludes them (§10.1: hand-back "continues").
        self.parked_s = 0.0

    # --- lease / console -------------------------------------------------------------------------------

    @property
    def console_url(self) -> str:
        return f"http://127.0.0.1:{self.console_port}"

    def _log_transition(self, event: dict[str, Any]) -> None:
        """Runs inside the lease lock on WHICHEVER thread requested the transition: only cheap flag
        work happens here (§11: the console thread never logs or touches Playwright). The owning
        thread writes the ``lease`` events in order via ``flush_lease_log``."""
        self._last_activity = time.monotonic()
        # Arm/disarm synchronously with the transition so a click right after "Take control" is never
        # dropped; the pump loop still does the Playwright-side work (bring_to_front, frame injection).
        if event["to"] == LeaseState.HUMAN_ACTIVE.value:
            self.recorder_armed = True
        elif event["to"] in (LeaseState.RESUMING.value, LeaseState.ABANDONED.value, LeaseState.AGENT_ACTIVE.value):
            self.recorder_armed = False

    def flush_lease_log(self) -> list[dict[str, Any]]:
        """Owning thread: write every not-yet-logged transition to run.jsonl, in order."""
        fresh = self.lease.unlogged(self._logged)
        for ev in fresh:
            self.log.event("lease", {"step_id": self.pending.step_id if self.pending else None, **ev})
        self._logged += len(fresh)
        return fresh

    def request(self, transition: str, by: str = "agent") -> LeaseState:
        """A transition requested by the OWNING thread (agent/system): applied and logged at once."""
        state = self.lease.request(transition, by=by)
        self.flush_lease_log()
        return state

    def close(self) -> None:
        """Teardown: stop the console server (so a later in-process run can bind the port) and flush."""
        self.flush_lease_log()
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001, S110
                pass
            self._server = None

    def start_console(self) -> None:
        if self._console_thread is not None or not self._start_console:
            return
        import logging

        from werkzeug.serving import make_server

        from anton.escalation.operator_app import create_app

        logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no request log on stderr, no banner on stdout
        app = create_app(self)
        try:
            server = make_server("127.0.0.1", self.console_port, app, threaded=True)
        except (OSError, SystemExit):  # werkzeug raises SystemExit on EADDRINUSE; never take the run down
            wanted = self.console_port
            server = make_server("127.0.0.1", 0, app, threaded=True)
            self.log.event("console", {"detail": f"port {wanted} in use; console bound to {server.server_port} instead"})
        self.console_port = server.server_port  # a 0 port becomes the bound one
        self._server = server
        self._console_thread = threading.Thread(target=server.serve_forever, name="anton-console", daemon=True)
        self._console_thread.start()

    # --- recorder ---------------------------------------------------------------------------------------

    def record_binding(self, source: Any, payload: dict[str, Any]) -> None:
        """``expose_binding`` callback (runs on the owning thread inside a pump). Dropped when disarmed."""
        if not self.recorder_armed:
            return
        frame_path: list[str] = []
        try:
            frame = source.get("frame") if isinstance(source, dict) else getattr(source, "frame", None)
            while frame is not None and frame.parent_frame is not None:
                parent = frame.parent_frame
                idx = parent.child_frames.index(frame) if frame in parent.child_frames else 0
                frame_path.insert(0, f"iframe[name={frame.name}]" if frame.name else f"iframe[{idx}]")  # same rule as snapshot.py
                frame = parent
        except Exception:  # noqa: BLE001 - a detached frame still yields a record
            pass
        self.record_human_action(dict(payload), frame_path)

    def record_human_action(self, payload: dict[str, Any], frame_path: list[str]) -> None:
        value = payload.get("value")
        label = payload.get("label_context") or payload.get("name") or ""
        if value is not None and (payload.get("input_type") == "password" or self.redactor.is_field_label(label)):
            value = "***"
        record = {
            "origin": "human", "ts": datetime.now(UTC).isoformat(), "event": payload.get("event"), "frame_path": frame_path, "url": payload.get("url"),
            "descriptor": {k: payload.get(k) for k in ("role", "name", "text", "control_type", "bbox", "label_context", "row_anchor", "col_header", "row_anchor_col_header")},
            "value": value, "checked": payload.get("checked"),
        }
        with self.human_actions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.redactor.scrub(json.loads(json.dumps(record, default=str)))) + "\n")
        self.human_action_count += 1
        self._last_activity = time.monotonic()
        self.log.event("human_action", {"step_id": self.pending.step_id if self.pending else None, "event": payload.get("event"), "target": self.redactor.scrub_text(str(payload.get("name") or payload.get("text") or ""))[:80], "frame_path": frame_path})

    # --- the park loop ----------------------------------------------------------------------------------------

    def next_request_id(self) -> str:
        self._seq += 1
        return f"{self.run_id}-i{self._seq}"

    def escalate(self, request: InterventionRequest) -> tuple[Resolution, InterventionRecord]:
        """Write intervention.json, PAUSE, start the console, print its URL, wait for a resolution."""
        self.pending = request
        seen = len(self.lease.history)  # everything after this index (the trigger included) is processed by _wait
        self.request("trigger", by="agent")  # §11: transition to PAUSED, THEN write the request
        self.log.write_json("intervention.json", request)
        self.start_console()
        print(f"anton: escalation {request.kind} at {request.step_id} — {request.reason[:120]}\n       operator console: {self.console_url}  (timeout {self.timeout_s}s)", file=sys.stderr, flush=True)
        self.log.event("intervention", {"step_id": request.step_id, "request_id": request.request_id, "kind": request.kind, "reason": request.reason, "console_url": self.console_url})
        return self._wait(request, seen)

    def _wait(self, request: InterventionRequest, seen: int) -> tuple[Resolution, InterventionRecord]:
        taken_at: datetime | None = None
        handed_back_at: datetime | None = None
        self._last_activity = time.monotonic()
        count_at_start = self.human_action_count
        t_park = time.monotonic()
        try:
            return self._drain_until_resolved(request, seen, count_at_start, taken_at, handed_back_at)
        finally:
            self.parked_s += time.monotonic() - t_park

    def _drain_until_resolved(self, request: InterventionRequest, seen: int, count_at_start: int, taken_at: datetime | None, handed_back_at: datetime | None) -> tuple[Resolution, InterventionRecord]:
        while True:
            try:
                self.pump(250)
            except Exception as e:  # noqa: BLE001 - the human closed the window / the driver died: abandon, never hang or orphan the record
                self.log.event("escalation_error", {"step_id": request.step_id, "detail": f"pump failed: {type(e).__name__}: {str(e)[:200]}"})
                try:
                    self.lease.request("abort", by="system")
                except LeaseError:
                    pass
            self.flush_lease_log()
            history = self.lease.unlogged(0)
            while seen < len(history):
                ev = history[seen]
                seen += 1
                to = ev["to"]
                if to == LeaseState.PAUSED.value:
                    continue  # our own trigger / re-escalate
                if to == LeaseState.HUMAN_ACTIVE.value:
                    taken_at = datetime.fromisoformat(ev["ts"])
                    self.recorder_armed = True
                    if self.on_take is not None:
                        try:
                            self.on_take()
                        except Exception as e:  # noqa: BLE001
                            self.log.event("recorder", {"phase": "arm_failed", "detail": str(e)[:200]})
                elif to == LeaseState.RESUMING.value:
                    handed_back_at = datetime.fromisoformat(ev["ts"])
                    if self.on_handback is not None:
                        self.on_handback()
                    return "resumed", self._record(request, "resumed", taken_at, handed_back_at, count_at_start)
                elif to == LeaseState.AGENT_ACTIVE.value and ev["transition"] in ("approve", "deny"):
                    res = "approved" if ev["transition"] == "approve" else "denied"
                    return res, self._record(request, res, taken_at, handed_back_at, count_at_start)
                elif to == LeaseState.ABANDONED.value:
                    res = "timed_out" if ev["transition"] == "timeout" else "abandoned"
                    return res, self._record(request, res, taken_at, handed_back_at, count_at_start)
            if self.lease.state is LeaseState.ABANDONED:  # terminal with nothing left to process: never spin
                return "abandoned", self._record(request, "abandoned", taken_at, handed_back_at, count_at_start)
            if time.monotonic() - self._last_activity > self.timeout_s:
                try:
                    self.request("timeout", by="system")
                except LeaseError:
                    pass

    def _record(self, request: InterventionRequest, resolution: str, taken_at: datetime | None, handed_back_at: datetime | None, count_at_start: int) -> InterventionRecord:
        n = self.human_action_count - count_at_start
        rec = InterventionRecord(
            request_id=request.request_id, kind=request.kind, at_step=request.step_id, reason=request.reason, requested_at=request.requested_at,
            control_taken_at=taken_at, handed_back_at=handed_back_at, resolved_at=datetime.now(UTC), resolution=resolution,  # type: ignore[arg-type]
            human_action_count=n, human_actions_path=self.log.relative(self.human_actions_path) if n else None,
        )
        self.records.append(rec)
        self.log.event("intervention", {"step_id": request.step_id, "request_id": request.request_id, "resolution": resolution, "human_action_count": n})
        return rec

    def re_escalate(self, request: InterventionRequest) -> tuple[Resolution, InterventionRecord]:
        """RESUMING → PAUSED once (the resume found no satisfied signature), then wait again."""
        self.pending = request
        seen = len(self.lease.history)
        self.request("re-escalate", by="agent")
        self.log.write_json("intervention.json", request)
        print(f"anton: re-escalation {request.kind} at {request.step_id} — {request.reason[:120]}\n       operator console: {self.console_url}", file=sys.stderr, flush=True)
        self.log.event("intervention", {"step_id": request.step_id, "request_id": request.request_id, "kind": request.kind, "reason": request.reason, "console_url": self.console_url, "re_escalation": True})
        return self._wait(request, seen)

    def grant_approval(self, issued_by: str = "operator") -> ApprovalToken:
        assert self.pending is not None
        token = ApprovalToken(action_id=self.pending.request_id, issued_by=issued_by, issued_at=datetime.now(UTC))
        self.approvals.grant(token)
        return token
