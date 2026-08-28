"""``.env`` loader (stdlib only). SPEC §2: secrets come from the environment; ``.env`` is the
gitignored local convenience the README demo path creates with ``cp .env.example .env``.

Rules: only ``KEY=value`` lines; ``#`` comments and blanks ignored; a variable that is already
set in the real environment is NEVER overridden; empty values are skipped; nothing is logged.
"""

from __future__ import annotations

import os
from pathlib import Path

from anton.schema.loader import repo_root

__all__ = ["load_dotenv"]


def load_dotenv(path: Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    """Load ``<repo>/.env`` (or ``path``) into ``env`` (default ``os.environ``); returns the names set."""
    target = os.environ if env is None else env
    p = path if path is not None else repo_root() / ".env"
    if not p.is_file():
        return []
    loaded: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or not value or key in target and target[key] != "":
            continue
        target[key] = value
        loaded.append(key)
    return loaded
