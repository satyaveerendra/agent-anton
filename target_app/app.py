"""Member Servicing Console — the stand-in legacy back-office app (SPEC §6).

Deliberately hostile markup (nested tables, generic class names, inline styles, no ids or
test ids, the detail pane inside an iframe), a signed-cookie session, pinned labels, tenant
variants (``TENANT=alpha|beta``) and injectable runtime conditions (``INJECT=`` env and/or
``?inject=`` query). Runtime-created sub-accounts live in memory; seed data is read-only.
Every interstitial is a DOM modal — the app never uses native ``alert``/``confirm``/``prompt``.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import Any

import yaml
from flask import (
    Blueprint,
    Flask,
    Response,
    abort,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

from target_app import seed

HERE = Path(__file__).parent
TENANTS_DIR = HERE / "tenants"
INJECTIONS = ("slow", "expire", "modal", "deny", "err500")
ONCE = {"expire", "modal"}  # fire once; the rest are sticky / every time


class Injector:
    """Armed injections and one-shot bookkeeping (process-level, in memory)."""

    def __init__(self, armed: set[str], slow_ms: int) -> None:
        self.armed = {a for a in armed if a in INJECTIONS}
        self.slow_ms = slow_ms

    def arm(self, name: str) -> None:
        if name in INJECTIONS:
            self.armed.add(name)

    def fire(self, name: str) -> bool:
        """True if ``name`` is armed; one-shot injections disarm themselves."""
        if name not in self.armed:
            return False
        if name in ONCE:
            self.armed.discard(name)
        return True


class State:
    """Read-only seed data plus runtime-created sub-accounts."""

    def __init__(self, data: dict) -> None:
        self.members: dict[str, dict] = {m["member_id"]: m for m in data["members"]}
        self.credentials: dict[str, str] = data["credentials"]
        self.created: dict[str, list[dict]] = {}

    def accounts(self, member_id: str) -> list[dict]:
        return list(self.members[member_id]["accounts"]) + list(self.created.get(member_id, []))

    def open_subaccount(self, member_id: str, account_type: str, nickname: str, deposit: float) -> dict:
        seq = len(self.created.get(member_id, [])) + 1
        acct = {
            "type": f"{account_type} (sub)",
            "account_no": f"7{member_id}{seq:03d}",
            "balance": float(deposit),
            "nickname": nickname,
        }
        self.created.setdefault(member_id, []).append(acct)
        return acct


def load_tenant(tenant: str) -> dict[str, Any]:
    path = TENANTS_DIR / f"{tenant}.yaml"
    if not path.exists():
        raise SystemExit(f"unknown tenant {tenant!r} (expected one of {[p.stem for p in TENANTS_DIR.glob('*.yaml')]})")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def money(value: float) -> str:
    return f"${value:,.2f}"


def create_app(
    tenant: str = "alpha",
    inject: set[str] | None = None,
    slow_ms: int = 5000,
    debug_state: bool = False,
    data_path: Path = seed.DATA_PATH,
) -> Flask:
    cfg = load_tenant(tenant)
    prefix: str = cfg.get("route_prefix", "") or ""
    app = Flask(
        __name__,
        template_folder=str(HERE / "templates"),
        static_folder=str(HERE / "static"),
        static_url_path=f"{prefix}/static",  # static lives under the tenant prefix too (beta: /portal/static)
    )
    # FAKE dev key for a local demo app; the session cookie is signed, nothing else depends on it.
    app.secret_key = "member-servicing-console-demo-key-not-a-secret"
    app.config["TENANT"] = cfg
    app.config["PREFIX"] = prefix
    app.config["DEBUG_STATE"] = debug_state
    app.extensions["state"] = State(seed.load(data_path))
    app.extensions["injector"] = Injector(inject or set(), slow_ms)
    app.jinja_env.filters["money"] = money
    app.jinja_env.globals["labels"] = cfg["labels"]
    app.jinja_env.globals["columns"] = cfg["accounts_columns"]
    app.jinja_env.globals["prefix"] = prefix

    bp = Blueprint("console", __name__, url_prefix=prefix or None)

    def state() -> State:
        return current_app.extensions["state"]

    def injector() -> Injector:
        return current_app.extensions["injector"]

    def p(path: str) -> str:
        return f"{prefix}{path}"

    @bp.before_request
    def _guard() -> Any:
        if (q := request.args.get("inject")) is not None:
            injector().arm(q)
        if request.endpoint in ("console.login", "console.logout"):
            return None
        if "user" not in session:
            return redirect(f"{p('/login')}?next={request.path}")
        g.user = session["user"]
        return None

    # --- login / logout --------------------------------------------------------------------

    @bp.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        nxt = request.values.get("next") or p("/search")
        if not nxt.startswith("/") or nxt.startswith("//") or (prefix and not nxt.startswith(prefix)):
            nxt = p("/search")
        error = None
        if request.method == "POST":
            creds = state().credentials
            if request.form.get("username") == creds["username"] and request.form.get("password") == creds["password"]:
                session.clear()
                session["user"] = creds["username"]
                return redirect(nxt)
            error = "Invalid username or password."
        expired = request.args.get("reason") == "expired"
        return render_template("login.html", next=nxt, error=error, expired=expired)

    @bp.route("/logout")
    def logout() -> Any:
        session.clear()
        return redirect(p("/login"))

    # --- search ------------------------------------------------------------------------------

    @bp.route("/search")
    def search() -> Any:
        member_id = (request.args.get("member_id") or "").strip()
        if member_id:
            return redirect(p(f"/member/{member_id}"))
        last_name = (request.args.get("last_name") or "").strip().lower()
        results = None
        if last_name:
            results = [m for m in state().members.values() if m["last_name"].lower() == last_name]
        modal = injector().fire("modal")
        return render_template("search.html", results=results, modal=modal)

    # --- member shell + pane -----------------------------------------------------------------

    @bp.route("/member/<member_id>")
    def member(member_id: str) -> Any:
        inj = injector()
        if inj.fire("expire"):
            session.clear()
            return redirect(f"{p('/login')}?next={request.path}&reason=expired")
        if "slow" in inj.armed:
            time.sleep(inj.slow_ms / 1000.0)
        m = state().members.get(member_id)
        if m is None:
            return render_template("not_found.html", member_id=member_id)
        return render_template("member.html", m=m)

    @bp.route("/member/<member_id>/pane")
    def pane(member_id: str) -> Any:
        m = state().members.get(member_id)
        if m is None:
            return Response(f"<html><body>No member found for {member_id}</body></html>", mimetype="text/html")
        return render_template("pane.html", m=m, accounts=state().accounts(member_id))

    # --- sub-account flow ---------------------------------------------------------------------

    @bp.route("/member/<member_id>/subaccount/new", methods=["GET", "POST"])
    def subaccount_new(member_id: str) -> Any:
        if request.method == "GET" and "err500" in injector().armed:
            return render_template("error500.html"), 500
        m = state().members.get(member_id)
        if m is None:
            return render_template("not_found.html", member_id=member_id)
        form = {"account_type": "Savings", "nickname": "", "initial_deposit": "", "terms": ""}
        errors: dict[str, str] = {}
        if request.method == "POST":
            form = {k: (request.form.get(k) or "") for k in form}
            try:
                deposit = float(form["initial_deposit"])
            except ValueError:
                deposit = -1.0
            # SPEC §6 pins exactly one server-side rule: the $25 minimum (decisions.md P2-6).
            if not math.isfinite(deposit) or deposit < 25:
                errors["initial_deposit"] = "Initial deposit must be at least $25."
            if not errors:
                session["pending_subaccount"] = {
                    "member_id": member_id,
                    "account_type": form["account_type"],
                    "nickname": form["nickname"].strip(),
                    "initial_deposit": deposit,
                }
                return redirect(p(f"/member/{member_id}/subaccount/review"))
        return render_template("subaccount_new.html", m=m, form=form, errors=errors)

    @bp.route("/member/<member_id>/subaccount/review", methods=["GET", "POST"])
    def subaccount_review(member_id: str) -> Any:
        m = state().members.get(member_id)
        pending = session.get("pending_subaccount")
        if m is None or not pending or pending.get("member_id") != member_id:
            return redirect(p(f"/member/{member_id}/subaccount/new"))
        if request.method == "POST":
            if "deny" in injector().armed:
                return render_template("permission_denied.html", m=m)
            acct = state().open_subaccount(member_id, pending["account_type"], pending["nickname"], pending["initial_deposit"])
            session.pop("pending_subaccount", None)
            session["last_account"] = acct
            return redirect(p(f"/member/{member_id}/subaccount/confirmation"))
        return render_template("review.html", m=m, pending=pending)

    @bp.route("/member/<member_id>/subaccount/confirmation")
    def subaccount_confirmation(member_id: str) -> Any:
        m = state().members.get(member_id)
        acct = session.get("last_account")
        if m is None or not acct:
            return redirect(p(f"/member/{member_id}"))
        return render_template("confirmation.html", m=m, acct=acct)

    app.register_blueprint(bp)

    # --- debug state (fixture-only; outside the allowlist by construction) ------------------

    if debug_state:

        @app.route("/__state/member/<member_id>")
        def debug_member_state(member_id: str) -> Any:
            st = app.extensions["state"]
            if member_id not in st.members:
                abort(404)
            return jsonify(
                {
                    "member_id": member_id,
                    "accounts": st.accounts(member_id),
                    "runtime_created": st.created.get(member_id, []),
                }
            )

    @app.errorhandler(404)
    def _plain_404(_: Exception) -> Response:
        return Response("404 Not Found\n", status=404, mimetype="text/plain")

    return app


def app_from_env() -> Flask:
    inject = {s.strip() for s in os.environ.get("INJECT", "").split(",") if s.strip()}
    return create_app(
        tenant=os.environ.get("TENANT", "alpha"),
        inject=inject,
        slow_ms=int(os.environ.get("SLOW_MS", "5000")),
        debug_state=os.environ.get("ANTON_TARGET_DEBUG") == "1",
    )


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    app = app_from_env()
    print(f"Member Servicing Console: tenant={app.config['TENANT']['tenant_id']} prefix={app.config['PREFIX'] or '/'} "
          f"inject={sorted(app.extensions['injector'].armed)} debug_state={app.config['DEBUG_STATE']} → http://{host}:{port}{app.config['PREFIX']}/login",
          flush=True)
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    serve(port=int(os.environ.get("PORT", "8000")))
