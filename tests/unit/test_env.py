"""``.env`` loader: real env wins, empties skipped, comments ignored, nothing echoed."""

from __future__ import annotations

from pathlib import Path

from anton.env import load_dotenv


def test_load_dotenv_never_overrides_real_env(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# comment\nANTHROPIC_API_KEY=from-file\nANTON_MODEL='claude-sonnet-5'\nEMPTY=\nexport X=1\nbroken line\n", encoding="utf-8")
    env = {"ANTHROPIC_API_KEY": "from-real-env", "ANTON_MODEL": ""}
    loaded = load_dotenv(f, env)
    assert loaded == ["X"]  # a real env var wins even when set to "" (explicitly unset)
    assert env["ANTON_MODEL"] == ""
    assert env["ANTHROPIC_API_KEY"] == "from-real-env" and env["X"] == "1"
    fresh: dict[str, str] = {}
    load_dotenv(f, fresh)
    assert fresh["ANTON_MODEL"] == "claude-sonnet-5"  # quotes stripped
    assert "EMPTY" not in env
    assert load_dotenv(tmp_path / "missing", {}) == []
