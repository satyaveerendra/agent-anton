"""Acceptance fixtures (SPEC §19): spawn the REAL target app as a subprocess on a free port and
talk to it over HTTP with a cookie jar — no mocking inside the verified path."""

from __future__ import annotations

import http.cookiejar
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from anton.schema.loader import repo_root

CREDS = {"username": "ops_user", "password": "demo-pass-1"}  # FAKE demo credential (SPEC §6)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib signature
        return None


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: str

    @property
    def location(self) -> str:
        return self.headers.get("Location", "")


@dataclass
class Client:
    """Minimal cookie-aware HTTP client that never follows redirects (so 302s are assertable)."""

    origin: str
    jar: http.cookiejar.CookieJar = field(default_factory=http.cookiejar.CookieJar)

    def _opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, data: dict[str, str] | None = None) -> Response:
        url = self.origin + path
        payload = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        if payload is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._opener().open(req, timeout=30) as r:
                return Response(r.status, dict(r.headers), r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers), e.read().decode("utf-8", "replace"))

    def get(self, path: str) -> Response:
        return self.request("GET", path)

    def post(self, path: str, data: dict[str, str]) -> Response:
        return self.request("POST", path, data)

    def login(self, prefix: str = "", next_path: str | None = None) -> Response:
        data = dict(CREDS)
        if next_path:
            data["next"] = next_path
        return self.post(f"{prefix}/login", data)


@dataclass
class Target:
    proc: subprocess.Popen
    origin: str
    prefix: str
    tenant: str
    log_path: Path

    def client(self) -> Client:
        return Client(self.origin)

    def cli_env(self) -> dict[str, str]:
        """The exports SPEC §19 says the fixture provides to CLI runs (used from phase 4)."""
        return {
            **os.environ,
            "ANTON_TARGET_ORIGIN": self.origin,
            "ANTON_TARGET_DEBUG": "1",
            "ANTON_SECRET_DEMO_PASSWORD": CREDS["password"],
        }

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def spawn_target(
    tenant: str = "alpha",
    inject: tuple[str, ...] = (),
    slow_ms: int | None = None,
    debug_state: bool = True,
    extra_env: dict[str, str] | None = None,
) -> Target:
    port = free_port()
    prefix = "/portal" if tenant == "beta" else ""
    cmd = [sys.executable, "-m", "anton", "target", "serve", "--tenant", tenant, "--port", str(port)]
    for name in inject:
        cmd += ["--inject", name]
    if slow_ms is not None:
        cmd += ["--slow-ms", str(slow_ms)]
    if debug_state:
        cmd.append("--debug-state")
    env = {**os.environ, "ANTON_TARGET_DEBUG": "1" if debug_state else "", **(extra_env or {})}
    log_dir = Path(tempfile.mkdtemp(prefix="anton-target-"))
    log_path = log_dir / f"target-{tenant}-{port}.log"
    log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - the process owns it; never a pipe (a full pipe would block the server)
    proc = subprocess.Popen(cmd, cwd=repo_root(), env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
    origin = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"target app exited early:\n{log_path.read_text(encoding='utf-8')}")
        try:
            with urllib.request.urlopen(f"{origin}{prefix}/login", timeout=2) as r:
                if r.status == 200:
                    return Target(proc, origin, prefix, tenant, log_path)
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("target app did not start in time")


@pytest.fixture(scope="module")
def alpha() -> Iterator[Target]:
    t = spawn_target("alpha")
    try:
        yield t
    finally:
        t.stop()


@pytest.fixture(scope="module")
def beta() -> Iterator[Target]:
    t = spawn_target("beta")
    try:
        yield t
    finally:
        t.stop()


@pytest.fixture
def target_factory() -> Iterator:
    """Spawn additional, short-lived target apps (one per injection) and stop them on teardown."""
    spawned: list[Target] = []

    def make(**kw) -> Target:  # noqa: ANN003 - passthrough to spawn_target
        t = spawn_target(**kw)
        spawned.append(t)
        return t

    try:
        yield make
    finally:
        for t in spawned:
            t.stop()


def seeded_members() -> list[dict]:
    import json

    return json.loads((repo_root() / "target_app" / "data.json").read_text(encoding="utf-8"))["members"]


DATA = Path(repo_root() / "target_app" / "data.json")
