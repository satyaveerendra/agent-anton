"""Synthetic member data for the Member Servicing Console (SPEC §6).

Everything here is FAKE by construction: names are NATO-alphabet placeholders, SSNs use the
invalid 900-XX-XXXX range, balances are random JSON numbers. The RNG seed is fixed, and the
canonical demo member ``204817`` and the AW-1b member ``310552`` are always emitted.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

DATA_PATH = Path(__file__).with_name("data.json")
CANONICAL_MEMBER_ID = "204817"
SECOND_MEMBER_ID = "310552"
SEED = 20260828
MEMBER_COUNT = 25

_FIRST = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India",
          "Juliet", "Kilo", "Lima", "Mike", "November", "Oscar", "Papa", "Quebec", "Romeo",
          "Sierra", "Tango", "Uniform", "Victor", "Whiskey", "Xray", "Yankee", "Zulu"]
_LAST = ["Sample", "Example", "Placeholder", "Testcase", "Fixture", "Dummy", "Mock", "Stub"]


def _member(rng: random.Random, member_id: str, idx: int) -> dict:
    first = _FIRST[idx % len(_FIRST)]
    last = _LAST[idx % len(_LAST)]
    dob = date(1950, 1, 1) + timedelta(days=rng.randint(0, 365 * 55))
    ssn = f"900-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999):04d}"
    checking = round(rng.uniform(50, 9_000), 2)
    savings = round(rng.uniform(100, 25_000), 2)
    return {
        "member_id": member_id,
        "first_name": first,
        "last_name": last,
        "dob": dob.isoformat(),
        "ssn": ssn,
        "accounts": [
            {"type": "Checking", "account_no": f"1{member_id}01", "balance": checking},
            {"type": "Savings", "account_no": f"2{member_id}01", "balance": savings},
        ],
    }


def generate() -> dict:
    rng = random.Random(SEED)
    ids: list[str] = [CANONICAL_MEMBER_ID, SECOND_MEMBER_ID]
    while len(ids) < MEMBER_COUNT:
        candidate = str(rng.randint(100_000, 999_999))
        if candidate not in ids:
            ids.append(candidate)
    members = [_member(rng, mid, i) for i, mid in enumerate(ids)]
    return {
        "note": "SYNTHETIC DATA — every name, SSN (900-XX-XXXX is an invalid range) and balance is fake.",
        "credentials": {"username": "ops_user", "password": "demo-pass-1", "note": "FAKE demo credential"},
        "members": members,
    }


def write(path: Path = DATA_PATH) -> Path:
    path.write_text(json.dumps(generate(), indent=2) + "\n", encoding="utf-8")
    return path


def load(path: Path = DATA_PATH) -> dict:
    if not path.exists():
        write(path)
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    out = write()
    print(f"wrote {out} ({MEMBER_COUNT} synthetic members; canonical {CANONICAL_MEMBER_ID}, second {SECOND_MEMBER_ID})")
