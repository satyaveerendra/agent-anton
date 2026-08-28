"""``make verify-evidence`` (SPEC §19): every seeded SSN and DOB (ISO form, as rendered), the fake demo
password and the ``sk-ant-`` key prefix must appear in NO file under ``evidence/``, ``artifacts/`` or
``runs/`` — text files line by line, binaries (screenshots) by byte scan. ``.env*`` files are never scanned.
Prints ``file:line`` for every hit; exit 1 on any.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from anton.schema.loader import repo_root

__all__ = ["needles", "scan", "main"]

SCAN_DIRS = ("evidence", "artifacts", "runs")


def needles(data_path: Path | None = None) -> dict[str, str]:
    """label → needle. SSNs and DOBs from the seed, plus the two constants."""
    data = json.loads((data_path or repo_root() / "target_app" / "data.json").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for m in data["members"]:
        out[f"ssn:{m['member_id']}"] = m["ssn"]
        out[f"dob:{m['member_id']}"] = m["dob"]
    out["demo-password"] = "demo-pass-1"
    out["api-key-prefix"] = "sk-ant-"
    return out


def _skip(path: Path) -> bool:
    return path.name.startswith(".env")


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head


def scan(root: Path | None = None, dirs: tuple[str, ...] = SCAN_DIRS) -> list[tuple[str, int, str]]:
    root = root or repo_root()
    hits: list[tuple[str, int, str]] = []
    wanted = needles(root / "target_app" / "data.json")
    for d in dirs:
        base = root / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or _skip(p):
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            rel = str(p.relative_to(root))
            if _is_binary(data[:8192]):
                for label, needle in wanted.items():  # a PNG/blob is scanned as bytes: any occurrence is a hit at "line 0"
                    if needle.encode("utf-8") in data:
                        hits.append((rel, 0, label))
                continue
            for i, line in enumerate(data.decode("utf-8", errors="ignore").splitlines(), start=1):
                for label, needle in wanted.items():
                    if needle in line:
                        hits.append((rel, i, label))
    return hits


def main(argv: list[str] | None = None) -> int:
    hits = scan()
    n_needles = len(needles())
    if hits:
        for path, line, label in hits:
            print(f"{path}:{line}: {label}")
        print(f"verify-evidence: FAIL — {len(hits)} occurrence(s) of {len({h[2] for h in hits})} needle(s)", file=sys.stderr)
        return 1
    print(f"verify-evidence: OK — {n_needles} needles (seeded SSNs + DOBs, demo password, key prefix) absent from {', '.join(SCAN_DIRS)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
