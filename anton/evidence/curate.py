"""``make curate-evidence``: copy the NEWEST run under ``runs/`` matching each SPEC §12 replay folder into
``evidence/<folder>/`` and regenerate the replay section of ``evidence/INDEX.md``.

Evidence is never hand-edited: a folder is a verbatim copy of a run directory produced by the code
(run.jsonl, result.json, screenshots, the redacted page snapshots, intervention.json when present).
The discovery / escalation-demo / tenant rows of INDEX.md are hand-written and kept; only the block
between the ``replay-evidence`` markers is generated.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from anton.schema.loader import repo_root

__all__ = ["FOLDERS", "curate", "main"]

START, END = "<!-- replay-evidence:start -->", "<!-- replay-evidence:end -->"


def _cap(r: dict) -> str:
    return r["capability_id"].split(".")[-1]


def _member(ev: list[dict]) -> str | None:
    run = next((e for e in ev if e["kind"] == "run"), None)
    return (run or {}).get("params", {}).get("member_id")


def _recoveries(r: dict) -> set[str]:
    return {x for s in r["steps"] for x in s["recoveries"]}


def _nav_duration(r: dict) -> int:
    return next((s["duration_ms"] for s in r["steps"] if s["step_id"] == "click_search"), 0)


def _fail(r: dict, kind: str) -> bool:
    return r["status"] == "FAILED" and (r.get("failure") or {}).get("kind") == kind


def _business(r: dict, code: str) -> bool:
    return r["status"] == "BUSINESS_OUTCOME" and r.get("outcome_code") == code


def _blocked(r: dict, kind: str) -> bool:
    return r["status"] == "BLOCKED" and (r.get("blocked") or {}).get("kind") == kind


def _alpha_plain(r: dict) -> bool:
    return r.get("tenant_id") == "alpha" and r.get("overlay_id") is None and not r.get("interventions")


#: folder → (gate, one-line description, matcher(result, events))
FOLDERS: dict[str, tuple[str, str, Callable[[dict, list[dict]], bool]]] = {
    "replay-success": ("AW-1", "happy-path replay of `read_savings_balance` for member 204817: SUCCESS, every targeted step tier ≤ 2, `__preconditions__` PASSED, typed output",
                       lambda r, ev: _cap(r) == "read_savings_balance" and r["status"] == "SUCCESS" and _member(ev) == "204817" and _alpha_plain(r) and not _recoveries(r) and _nav_duration(r) < 5000),
    "replay-second-member": ("AW-1b", "the same artifact with `member_id=310552`: a capability, not a macro (that member's balance)",
                             lambda r, ev: _cap(r) == "read_savings_balance" and r["status"] == "SUCCESS" and _member(ev) == "310552" and _alpha_plain(r)),
    "replay-not-found": ("AW-2", "member 999999: `BUSINESS_OUTCOME MEMBER_NOT_FOUND` at `click_search` (detector short-circuits the wait), exit 10, no traceback",
                         lambda r, ev: _cap(r) == "read_savings_balance" and _business(r, "MEMBER_NOT_FOUND")),
    "replay-session-expiry": ("AW-3a", "`INJECT=expire`: the `session_expired` detector ran the login flow once and the run resumed via `resume_point` — exactly one step carries `recoveries: [\"session_expired\"]`",
                              lambda r, ev: r["status"] == "SUCCESS" and "session_expired" in _recoveries(r)),
    "replay-modal": ("AW-3b", "`INJECT=modal`: the maintenance notice was dismissed by the `announcement_modal` recovery (`Dismiss`) and the run succeeded",
                     lambda r, ev: r["status"] == "SUCCESS" and "announcement_modal" in _recoveries(r)),
    "replay-slow": ("AW-3c", "`INJECT=slow` (5 s): the declarative wait absorbed it — the navigating step has `duration_ms ≥ 5000`, `attempts: 1`, no recovery",
                    lambda r, ev: _cap(r) == "read_savings_balance" and r["status"] == "SUCCESS" and _alpha_plain(r) and not _recoveries(r) and _nav_duration(r) >= 5000),
    "replay-hard-failure": ("AW-4", "`INJECT=err500` on `open_subaccount`: `FAILED APP_ERROR` at `click_open_sub_account` with `FailureDetail` (expected postcondition, observed summary, masked screenshot + redacted page snapshot in `failure.evidence`); the member-page observation shows pane cells as `***`",
                            lambda r, ev: _cap(r) == "open_subaccount" and _fail(r, "APP_ERROR")),
    "replay-escalation-abandoned": ("AW-5a", "`--escalate` without consent: parked (APPROVAL, `intervention.json`), operator took control then aborted via the console → `BLOCKED ESCALATION_UNRESOLVED`, `resolution: abandoned`, lease log `AGENT_ACTIVE→PAUSED→HUMAN_ACTIVE→ABANDONED`, no action after PAUSED",
                                    lambda r, ev: _blocked(r, "ESCALATION_UNRESOLVED") and bool(r.get("interventions")) and r["interventions"][0]["resolution"] == "abandoned"),
    "replay-blocked-approval": ("AW-7a", "`open_subaccount` without a consent flag: `BLOCKED APPROVAL_REQUIRED` at the RISKY confirm step before any action (gate `NeedsApproval`, no `action` event after it)",
                                lambda r, ev: _cap(r) == "open_subaccount" and _blocked(r, "APPROVAL_REQUIRED") and not r.get("interventions")),
    "replay-subaccount-approved": ("AW-7b", "`--approve-risky`: SUCCESS; `result.json` shows `account_number: \"***\"` (only `--outputs-file` carries the raw value); `approval_used{by: caller}` in run.jsonl",
                                   lambda r, ev: _cap(r) == "open_subaccount" and r["status"] == "SUCCESS" and _alpha_plain(r)),
    "replay-permission-denied": ("AW-7c", "`INJECT=deny --approve-risky`: `BUSINESS_OUTCOME PERMISSION_DENIED` at the RISKY step (no retry on an irreversible action)",
                                 lambda r, ev: _business(r, "PERMISSION_DENIED")),
    "replay-validation-error": ("AW-7d", "`initial_deposit=10`: `BUSINESS_OUTCOME VALIDATION_ERROR` at `click_continue_to_review` with the inline message as `outcome_detail`",
                                lambda r, ev: _business(r, "VALIDATION_ERROR")),
    "replay-draft-refused": ("AW-7e", "a draft copy of the artifact with `--allow-draft --approve-risky`: `FAILED POLICY_VIOLATION` (`DRAFT_RISKY`) at start-up — a run directory with `result.json` but no browser launch, exit 30",
                             lambda r, ev: _fail(r, "POLICY_VIOLATION") and "DRAFT_RISKY" in (r.get("failure") or {}).get("observed_summary", "")),
    "replay-tenant-beta": ("AW-6", "the alpha-recorded `read_savings_balance` on tenant beta through `overlays/beta.yaml`: `tenant_id/overlay_id: beta`, `fill_member_id` at tier 1 with `resolution.source: \"overlay\"`, the `row_cell` extraction surviving the column shift, masks resolved (`screenshot` event)",
                           lambda r, ev: _cap(r) == "read_savings_balance" and r["status"] == "SUCCESS" and r.get("overlay_id") == "beta"),
}

COPY_FILES = ("run.jsonl", "result.json", "intervention.json", "human_actions.jsonl", "discovery.json")


def _load(run_dir: Path) -> tuple[dict, list[dict]] | None:
    try:
        result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError):
        return None
    if "capability_id" not in result:
        return None  # a discovery run
    return result, events


def curate(root: Path | None = None) -> dict[str, str | None]:
    root = root or repo_root()
    runs = sorted((p for p in (root / "runs").iterdir() if p.is_dir() and (p / "result.json").exists()), key=lambda p: p.name, reverse=True)
    loaded = [(p, *lr) for p in runs if (lr := _load(p)) is not None]
    chosen: dict[str, str | None] = {}
    for folder, (_gate, _desc, match) in FOLDERS.items():
        pick = next((p for p, r, ev in loaded if match(r, ev)), None)
        chosen[folder] = pick.name if pick else None
        if pick is None:
            continue
        dst = root / "evidence" / folder
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        for name in COPY_FILES:
            if (pick / name).exists():
                shutil.copy2(pick / name, dst / name)
        for snap in pick.glob("snapshot-*.json"):
            shutil.copy2(snap, dst / snap.name)
        if (pick / "screenshots").exists():
            shutil.copytree(pick / "screenshots", dst / "screenshots")
    _write_index(root, chosen)
    return chosen


def _write_index(root: Path, chosen: dict[str, str | None]) -> None:
    index = root / "evidence" / "INDEX.md"
    text = index.read_text(encoding="utf-8") if index.exists() else "# Evidence index\n"
    rows = ["", START, "", "## Replay evidence (generated by `make curate-evidence` from the newest matching run under `runs/`)", "",
            "| folder | gate | run | what it shows |", "|---|---|---|---|"]
    for folder, (gate, desc, _m) in FOLDERS.items():
        run = chosen.get(folder)
        rows.append(f"| `{folder}/` | {gate} | `{run}` | {desc} |" if run else f"| `{folder}/` | {gate} | — | (no matching run under `runs/` yet — run the AW target first) |")
    rows += ["", "Each folder is a verbatim copy of that run's `run.jsonl`, `result.json`, masked screenshots and redacted page snapshots (plus `intervention.json` where an intervention occurred); `make verify-evidence` scans all of it.", "", END, ""]
    block = "\n".join(rows)
    if START in text and END in text:
        text = text[: text.index(START)].rstrip("\n") + "\n" + block + text[text.index(END) + len(END):].lstrip("\n")
    else:
        text = text.rstrip("\n") + "\n" + block
    index.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    chosen = curate()
    missing = [f for f, r in chosen.items() if r is None]
    for folder, run in chosen.items():
        print(f"{folder:<30} {run or '— no matching run'}")
    if missing:
        print(f"curate-evidence: {len(missing)} folder(s) without a matching run: {missing}", file=sys.stderr)
        return 1
    print(f"curate-evidence: {len(chosen)} folders refreshed; evidence/INDEX.md updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
