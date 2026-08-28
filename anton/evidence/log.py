"""``runs/<run_id>/run.jsonl`` writer (SPEC §12).

One JSON event per line, every value scrubbed by the Redactor before it touches disk. The
``observation`` event carries the full redacted enumeration so a failure is debuggable from the
log alone; ``write_json`` writes the side files (``result.json``, the failure page snapshot).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from anton.policy.redact import Redactor
from anton.schema.snapshot import SemanticSnapshot

__all__ = ["RunLog"]


class RunLog:
    def __init__(self, run_dir: Path, redactor: Redactor) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.path = self.run_dir / "run.jsonl"
        self._f = self.path.open("a", encoding="utf-8")
        self._seq = 0
        self._lock = threading.Lock()

    # --- events --------------------------------------------------------------------------------

    RESERVED = frozenset({"seq", "ts", "kind"})

    def event(self, kind: str, data: dict[str, Any]) -> None:
        """Append one event. The envelope keys (``seq``/``ts``/``kind``) always win: a payload key
        that collides is stored under ``data_<key>`` so an event can never masquerade as another."""
        plain = json.loads(json.dumps(data, default=str))  # stringify exotic values FIRST, then scrub
        payload = {(f"data_{k}" if k in self.RESERVED else k): v for k, v in self.redactor.scrub(plain).items()}
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "ts": datetime.now(UTC).isoformat(), "kind": kind, **payload}
            self._f.write(json.dumps(record, sort_keys=False) + "\n")
            self._f.flush()

    def observation(self, step_id: str, snapshot: SemanticSnapshot) -> None:
        self.event(
            "observation",
            {
                "step_id": step_id,
                "url": snapshot.url,
                "title": snapshot.title,
                "frame_count": snapshot.frame_count,
                "elements": [e.model_dump(mode="json") for e in snapshot.elements],
            },
        )

    # --- side files ------------------------------------------------------------------------------

    def write_json(self, name: str, obj: Any) -> Path:
        """Write a redacted JSON side file; returns the path RELATIVE to the run dir."""
        payload = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else json.loads(json.dumps(obj, default=str))
        out = self.run_dir / name
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(self.redactor.scrub(payload), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, out)  # atomic: a reader (the console, a test) never sees a torn file
        return out

    def relative(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.run_dir.resolve()))
        except ValueError:
            return str(path)

    def close(self) -> None:
        self._f.close()
