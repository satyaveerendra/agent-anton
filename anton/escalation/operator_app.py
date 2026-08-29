"""Operator console (SPEC §11) — plain Flask, no styling effort. Runs in a daemon thread and MUST NOT
touch any Playwright object: handlers call ``Lease.request`` and read already-written files.
"""

from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, jsonify, send_file

from anton.policy.gate import LeaseState

if TYPE_CHECKING:
    from anton.escalation.controller import EscalationController

__all__ = ["create_app"]


def create_app(ctl: EscalationController) -> Flask:
    app = Flask("anton-console")

    def state() -> dict[str, Any]:
        snap = ctl.lease.snapshot()
        pending = ctl.pending.model_dump(mode="json") if ctl.pending is not None and ctl.lease.state.value != "AGENT_ACTIVE" and ctl.lease.state.value != "ABANDONED" else None
        return {**snap, "pending": pending}

    def transition(name: str, *, approval_only: bool = False) -> Response:
        from anton.escalation.controller import LeaseError

        if approval_only and (ctl.pending is None or ctl.pending.kind != "APPROVAL"):
            return Response(json.dumps({"error": f"{name} is valid only for an APPROVAL intervention"}), status=409, mimetype="application/json")
        try:
            if name == "approve":
                # Grant BEFORE the transition (the owning thread may consume the token the instant the
                # lease flips), but only from PAUSED — and revoke it if the transition is refused.
                if ctl.lease.state is not LeaseState.PAUSED:
                    raise LeaseError(f"'approve' is not valid from {ctl.lease.state.value}")
                token = ctl.grant_approval()
                try:
                    new = ctl.lease.request(name, by="operator")
                except LeaseError:
                    ctl.approvals.consume(token.action_id)
                    raise
            else:
                new = ctl.lease.request(name, by="operator")
        except LeaseError as e:
            return Response(json.dumps({"error": str(e), **ctl.lease.snapshot()}), status=409, mimetype="application/json")
        return jsonify({"ok": True, "lease": new.value, **state()})

    @app.get("/api/state")
    def api_state() -> Response:
        return jsonify(state())

    @app.post("/api/take")
    def api_take() -> Response:
        return transition("take")

    @app.post("/api/handback")
    def api_handback() -> Response:
        return transition("handback")

    @app.post("/api/approve")
    def api_approve() -> Response:
        return transition("approve", approval_only=True)

    @app.post("/api/deny")
    def api_deny() -> Response:
        return transition("deny", approval_only=True)

    @app.post("/api/abort")
    def api_abort() -> Response:
        return transition("abort")

    @app.get("/screenshot")
    def screenshot() -> Response:
        if ctl.pending is None:
            return Response("no pending intervention", status=404)
        path = ctl.run_dir / ctl.pending.screenshot
        if not path.is_file():
            return Response("screenshot missing", status=404)
        return send_file(path, mimetype="image/png")

    @app.get("/")
    def index() -> Response:
        s = state()
        p = s["pending"]
        esc = html.escape
        body = [f"<h1>anton operator console</h1><p>run <code>{esc(ctl.run_id)}</code> · lease <b>{esc(s['lease'])}</b> · owner {esc(s['owner'])}</p>"]
        if p is None:
            body.append("<p>No pending intervention.</p>")
        else:
            fp = p.get("target_fingerprint") or {}
            body.append(
                f"<h2>{esc(p['kind'])} at step <code>{esc(p['step_id'])}</code></h2>"
                f"<p><b>capability:</b> {esc(str(p.get('capability_id') or ''))}</p>"
                + (f"<p><b>goal:</b> {esc(str(p['goal']))}</p>" if p.get('goal') else "")
                +
                f"<p><b>reason:</b> {esc(p['reason'])}</p>"
                f"<p><b>target fingerprint:</b> <code>{esc(json.dumps(fp))}</code><br><b>rationale:</b> {esc(str(p.get('target_rationale') or ''))}</p>"
                f"<p><b>expected signature:</b> <code>{esc(json.dumps(p.get('expected')))}</code></p>"
                f"<p><b>page signature:</b> <code>{esc(p['page_signature'])}</code></p>"
                f"<p><img src='/screenshot' style='max-width:900px;border:1px solid #999' alt='masked screenshot'></p>"
            )
            buttons = [("take", "Take control"), ("handback", "Hand back"), ("abort", "Abort")]
            if p["kind"] == "APPROVAL":
                buttons = [("approve", "Approve once"), ("deny", "Deny")] + buttons
            # The action row goes ABOVE the (tall) screenshot so it is never below the fold.
            body.insert(1, "<p><b>actions:</b> " + " ".join(f"<button onclick=\"fetch('/api/{n}',{{method:'POST'}}).then(r=>r.json()).then(j=>{{document.getElementById('out').textContent=JSON.stringify(j)}}).then(()=>setTimeout(()=>location.reload(),800))\">{esc(label)}</button>" for n, label in buttons) + "</p><pre id='out'></pre>")
        body.append("<p><small>Drive the headed browser window the run owns; this page only changes the lease.</small></p>")
        return Response("<html><body style='font-family:sans-serif'>" + "".join(body) + "</body></html>", mimetype="text/html")

    return app
