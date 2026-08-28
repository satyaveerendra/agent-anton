"""SPEC §16 "Distiller": binding, canonicalization, secrets, ladders, extraction, landmark rule,
FieldValue/Checked postconditions, inherited codes, READ pruning, deterministic ids, draft."""

from __future__ import annotations

import pytest

from anton.discovery.describe import describe
from anton.distill.distiller import (
    DistillContext,
    DistillError,
    RecordedTurn,
    bind_tokens,
    bind_value,
    canonical_path_regex,
    distill,
    ladder_for,
    landmark_text,
    step_id_for,
)
from anton.schema.artifact import (
    ActionType,
    BBoxLocator,
    Checked,
    ElementVisible,
    FieldValue,
    LabelRelativeLocator,
    LiteralValue,
    ParamRef,
    ParamSpec,
    ParamType,
    RiskClass,
    RoleNameLocator,
    SecretRef,
    TextVisible,
    UrlMatches,
)
from anton.schema.snapshot import BBox, ElementDescriptor, SemanticSnapshot, SnapshotElement, TurnRecord
from anton.schema.spec import CapabilitySpec, OutputDecl
from tests.conftest import make_profile

PARAMS = {"member_id": "204817", "initial_deposit": "50", "nickname": "Holiday fund"}
VIEW = (1280, 900)


def test_whole_token_binding_never_over_binds() -> None:
    assert bind_tokens("Member 204817 profile", PARAMS) == "Member $param:member_id profile"
    assert bind_tokens("$50.00", PARAMS) == "$50.00"  # `50` inside `$50.00` is not a whole token
    assert bind_tokens("Holiday fund", PARAMS) == "$param:nickname"  # a multi-word value binds as a whole token run
    assert bind_tokens("My Holiday fund account", PARAMS) == "My $param:nickname account"
    assert bind_tokens("Holiday funds", PARAMS) == "Holiday funds" and bind_tokens("204817x", PARAMS) == "204817x"
    assert bind_tokens("Member (204817)", PARAMS) == "Member ($param:member_id)"  # word boundaries, not whitespace
    assert bind_tokens("Member 204817, profile", PARAMS) == "Member $param:member_id, profile"
    assert bind_tokens("50.00", PARAMS) == "50.00" and bind_tokens("$50", PARAMS) == "$50" and bind_tokens("1204817", PARAMS) == "1204817"
    assert bind_tokens("", PARAMS) == ""
    assert bind_value("204817", PARAMS) == ParamRef(name="member_id")
    assert bind_value("50.00", PARAMS) == ParamRef(name="initial_deposit")  # numbers compare after normalize
    assert bind_value("$50", PARAMS) == ParamRef(name="initial_deposit")
    assert bind_value("Holiday fund", PARAMS) == ParamRef(name="nickname")  # typed value: exact match binds
    assert bind_value("500", PARAMS) == LiteralValue(value="500")


def test_url_canonicalization_and_prefix_stripping() -> None:
    assert canonical_path_regex("/member/204817", PARAMS) == "^/member/[^/]+$"
    assert canonical_path_regex("/portal/member/204817/subaccount/new", PARAMS, "/portal") == "^/member/[^/]+/subaccount/new$"
    assert canonical_path_regex("http://x/search?q=1", PARAMS) == "^/search$"
    assert canonical_path_regex("/member/12345", {}) == "^/member/[^/]+$"  # numeric segments generalize too


def test_step_ids_are_deterministic_with_collisions() -> None:
    used: set[str] = set()
    assert step_id_for(ActionType.CLICK, "Search", used) == "click_search"
    assert step_id_for(ActionType.CLICK, "Search", used) == "click_search_2"
    assert step_id_for(ActionType.FILL, "Member ID", used) == "fill_member_id"
    assert step_id_for(ActionType.CLICK, "Confirm and open account now please", used) == "click_confirm_and_open_account"
    assert step_id_for(ActionType.NAVIGATE, None, used, path="http://x/member/204817/subaccount/new", params=PARAMS) == "navigate_member_subaccount_new"
    assert step_id_for(ActionType.NAVIGATE, None, used, path="/search") == "navigate_search"
    assert step_id_for(ActionType.PRESS, None, used, key="Enter") == "press_enter"


def cell(ref: str, text: str, x: float, y: float, w: float = 100, h: float = 25, frame: list[str] | None = None, role: str = "cell", name: str | None = None, ct: str = "td", **kw) -> SnapshotElement:
    return SnapshotElement(ref=ref, role=role, name=text if name is None else name, text=text, control_type=ct, frame_path=frame or [], bbox=BBox(x=x, y=y, w=w, h=h), enabled=True, visible=True, **kw)


PANE = ["iframe[name=detail]"]


def member_page() -> SemanticSnapshot:
    els = [
        cell("h1", "Member Profile", 10, 10, role="text", ct="span"), cell("h2", "Signed in as ops_user", 900, 5, role="text", ct="span"),
        cell("l1", "Sign out", 1100, 5, role="link", ct="a"),
        # profile table (touching rows) then a gap, then the accounts table with a header row
        cell("p1", "Name", 20, 100, frame=PANE), cell("p2", "Alpha Sample", 130, 100, frame=PANE),
        cell("p3", "SSN", 20, 125, frame=PANE), cell("p4", "***", 130, 125, frame=PANE),
        cell("t1", "Accounts", 20, 172, frame=PANE),  # a one-cell title row touching the header row (real layout)
        cell("hh1", "Type", 20, 200, frame=PANE), cell("hh2", "Account No.", 130, 200, frame=PANE), cell("hh3", "Balance", 240, 200, frame=PANE),
        cell("r1", "Checking", 20, 225, frame=PANE), cell("r2", "***", 130, 225, frame=PANE), cell("r3", "$5,577.90", 240, 225, frame=PANE),
        cell("s1", "Savings", 20, 250, frame=PANE), cell("s2", "***", 130, 250, frame=PANE), cell("s3", "$19,823.28", 240, 250, frame=PANE),
    ]
    return SemanticSnapshot(url="http://x/member/204817", title="Member Profile — Console", frame_count=2, elements=els)


def search_page(value: str = "") -> SemanticSnapshot:
    els = [
        cell("h1", "Member Search", 10, 10, role="text", ct="span"), cell("h2", "Signed in as ops_user", 900, 5, role="text", ct="span"),
        cell("l1", "Sign out", 1100, 5, role="link", ct="a"),
        cell("c1", "Member ID", 20, 60), cell("i1", "", 130, 60, role="textbox", name="Member ID", ct="input", value=value),
        cell("c2", "Terms", 20, 90), cell("k1", "", 130, 90, role="checkbox", name="Terms", ct="input", checked=False),
        cell("b1", "Search", 130, 120, role="button", ct="button"),
    ]
    return SemanticSnapshot(url="http://x/search", title="Member Search — Console", frame_count=1, elements=els)


def test_describe_finds_table_headers_and_labels() -> None:
    snap = member_page()
    d = describe(snap, next(e for e in snap.elements if e.ref == "s3"))
    assert d.row_anchor == "Savings" and d.row_anchor_col_header == "Type" and d.col_header == "Balance"
    assert d.label_context == "Savings"  # the masked account-number cell is skipped as a label
    d2 = describe(snap, next(e for e in snap.elements if e.ref == "p2"))
    assert d2.col_header is None and d2.label_context == "Name"  # the profile table has no header row
    s = search_page()
    d3 = describe(s, next(e for e in s.elements if e.ref == "i1"))
    assert d3.label_context == "Member ID" and d3.col_header is None


def test_ladders_extraction_rule_and_binding() -> None:
    snap = member_page()
    d = describe(snap, next(e for e in snap.elements if e.ref == "s3"))
    lad = ladder_for(d, VIEW, PARAMS, extraction=True)
    assert not any(isinstance(c, RoleNameLocator) for c in lad.candidates)  # never the value as a name
    rc = next(c for c in lad.candidates if isinstance(c, LabelRelativeLocator))
    assert rc.relation == "row_cell" and rc.anchor_text == "Savings" and rc.anchor_col_header == "Type" and rc.col_header == "Balance" and rc.frame_path == PANE
    assert lad.fingerprint.name_snippet is None and lad.fingerprint.text_snippet is None and lad.fingerprint.control_type == "td"
    assert isinstance(lad.candidates[-1], BBoxLocator) and 0 < lad.candidates[-1].x < 1
    s = search_page()
    di = describe(s, next(e for e in s.elements if e.ref == "i1"))
    lad2 = ladder_for(di, VIEW, PARAMS)
    assert isinstance(lad2.candidates[0], RoleNameLocator) and lad2.candidates[0].name == "Member ID" and lad2.candidates[0].role == "textbox"
    rl = lad2.candidates[1]
    assert isinstance(rl, LabelRelativeLocator) and rl.relation == "right_of_label" and rl.anchor_text == "Member ID" and rl.target_role == "textbox"
    bound = ladder_for(ElementDescriptor(role="link", name="204817", text="204817", control_type="a", bbox=BBox(x=1, y=1, w=1, h=1)), VIEW, PARAMS)
    assert bound.candidates[0].name == "$param:member_id" and bound.fingerprint.name_snippet == "$param:member_id"  # type: ignore[union-attr]


def test_landmark_rule_excludes_param_and_read_values() -> None:
    before, after = search_page(), member_page()
    after.elements.insert(0, cell("h0", "Latest balance $19,823.28 for 204817", 10, 2, role="text", ct="span"))  # topmost, but quotes a read AND a param value
    assert landmark_text(before, after, ["204817", "$19,823.28"]) == "Member Profile"
    assert landmark_text(before, after, ["$19,823.28"]) == "Member Profile"
    assert landmark_text(before, after, []) == "Latest balance $19,823.28 for 204817"  # without exclusions the topmost wins
    assert landmark_text(before, after, ["Member Profile", "$19,823.28"]) is None
    assert landmark_text(after, after, []) is None


def rec(turn: int, tool: str, digest: str, **kw) -> TurnRecord:
    base = dict(turn=turn, digest_path=digest, screenshot_path="s.png", rationale=f"turn {turn}", tool=tool, args={}, gate_decision="n/a", page_signature_after="sig")
    base.update(kw)
    return TurnRecord(**base)


def spec() -> CapabilitySpec:
    return CapabilitySpec(
        capability_id="servicing.read_savings_balance", name="Read savings balance", description="d",
        inputs=[ParamSpec(name="member_id", type=ParamType.STRING, description="id", pattern="^[0-9]+$")],
        outputs=[OutputDecl(name="savings_balance", type="NUMBER", description="balance")],
        business_outcomes={"MEMBER_NOT_FOUND": "No member"},
    )


def recorded_balance_run(*, with_read: bool = True, checkbox: bool = False) -> list[RecordedTurn]:
    s0, s1, s2 = search_page(), search_page("204817"), member_page()
    input_desc = describe(s0, next(e for e in s0.elements if e.ref == "i1"))
    btn_desc = describe(s1, next(e for e in s1.elements if e.ref == "b1"))
    turns = [
        RecordedTurn(rec(1, "act", "d1", args={"ref": "i1", "action": "FILL", "value": "$param:member_id"}, gate_decision="Allow:SAFE", executed=input_desc, action=ActionType.FILL, value_ref=ParamRef(name="member_id")), s0, s1, "/search", "/search", VIEW),
    ]
    n = 2
    if checkbox:
        s1b = search_page("204817")
        next(e for e in s1b.elements if e.ref == "k1").checked = True
        k_desc = describe(s1, next(e for e in s1.elements if e.ref == "k1"))
        turns.append(RecordedTurn(rec(n, "act", "dk", args={"ref": "k1", "action": "CLICK"}, gate_decision="Allow:SAFE", executed=k_desc, action=ActionType.CLICK), s1, s1b, "/search", "/search", VIEW))
        s1 = s1b
        n += 1
    if with_read:
        turns.append(RecordedTurn(rec(n, "read", "d2", args={"ref": "i1"}, executed=input_desc), s1, s1, "/search", "/search", VIEW))
        n += 1
    turns.append(RecordedTurn(rec(n, "act", "d3", args={"ref": "b1", "action": "CLICK"}, gate_decision="Allow:SAFE", executed=btn_desc, action=ActionType.CLICK), s1, s2, "/search", "/member/204817", VIEW))
    n += 1
    turns.append(RecordedTurn(rec(n, "finish", "d4", args={"outputs": {"savings_balance": "s3"}}), s2, None, "/member/204817", None, VIEW))
    return turns


def ctx(**kw) -> DistillContext:
    base = dict(spec=spec(), profile=make_profile(), profile_path="profiles/servicing.yaml", params={"member_id": "204817"}, entry_url="http://localhost:8000/search", route_prefix="", run_id="run-1", model="claude-sonnet-5", read_values=["$19,823.28"])
    base.update(kw)
    return DistillContext(**base)


def test_distill_balance_run_end_to_end() -> None:
    art = distill(recorded_balance_run(), ctx())
    assert art.status == "draft" and art.provenance.origin == "discovery" and art.provenance.discovery_run_id == "run-1" and art.provenance.model == "claude-sonnet-5"
    assert [s.id for s in art.steps] == ["fill_member_id", "click_search"]  # READ turn pruned, ids deterministic
    fill, click = art.steps
    assert fill.value == ParamRef(name="member_id") and any(isinstance(p, FieldValue) and p.expected == ParamRef(name="member_id") for p in fill.postcondition.all_of)
    kinds = {type(p) for p in click.postcondition.all_of}
    assert kinds == {UrlMatches, TextVisible}
    assert next(p for p in click.postcondition.all_of if isinstance(p, UrlMatches)).regex == "^/member/[^/]+$"
    assert next(p for p in click.postcondition.all_of if isinstance(p, TextVisible)).regex == "Member Profile"
    out = art.outputs[0]
    assert out.name == "savings_balance" and out.extraction.normalize == "money" and out.extraction.attr == "text"
    rc = next(c for c in out.extraction.locator.candidates if isinstance(c, LabelRelativeLocator))
    assert rc.relation == "row_cell" and rc.col_header == "Balance" and rc.anchor_text == "Savings"
    assert art.business_outcomes["MEMBER_NOT_FOUND"] == "No member" and {"VALIDATION_ERROR", "PERMISSION_DENIED"} <= set(art.business_outcomes)  # inherited codes
    assert art.requires_flows == ["login"] and any(isinstance(p, UrlMatches) and p.regex == "^/search$" for p in art.preconditions.all_of)
    assert any(isinstance(p, ElementVisible) for p in art.preconditions.all_of)
    assert any(isinstance(p, ElementVisible) and p.ladder == out.extraction.locator for p in art.success.all_of)
    assert art.inputs == spec().inputs and art.app_profile == "profiles/servicing.yaml" and art.provenance.recorded_against.tenant_id == "alpha"


def test_distill_checkbox_click_gets_checked_and_risky_from_gate() -> None:
    turns = recorded_balance_run(checkbox=True)
    turns[-2].record.gate_decision = "Allow:RISKY"
    art = distill(turns, ctx())
    ids = [s.id for s in art.steps]
    assert ids == ["fill_member_id", "click_terms", "click_search"]
    assert any(isinstance(p, Checked) and p.expected is True for p in art.steps[1].postcondition.all_of)
    assert art.steps[2].risk is RiskClass.RISKY and art.steps[2].retries == 0 and art.steps[2].requires_approval
    # an UNcheck records Checked(False)
    turns = recorded_balance_run(checkbox=True)
    next(e for e in turns[1].before.elements if e.ref == "k1").checked = True
    next(e for e in turns[1].after.elements if e.ref == "k1").checked = False
    art2 = distill(turns, ctx())
    assert any(isinstance(p, Checked) and p.expected is False for p in art2.steps[1].postcondition.all_of)


def test_human_takeover_run_is_draft_and_says_so() -> None:
    art = distill(recorded_balance_run(), ctx(human_takeover=True))
    assert art.status == "draft" and "human took over" in art.description
    assert distill(recorded_balance_run(), ctx()).status == "draft" and "human took over" not in distill(recorded_balance_run(), ctx()).description


def test_distill_secret_handling_and_errors() -> None:
    turns = recorded_balance_run()
    turns[0].record.value_ref = SecretRef(name="X")
    art = distill(turns, ctx())
    assert art.steps[0].value == SecretRef(name="X")
    turns[0].record.value_ref = LiteralValue(value="hunter2")
    with pytest.raises(DistillError, match="secret"):
        distill(turns, ctx(secret_values={"hunter2"}))
    with pytest.raises(DistillError, match="did not finish"):
        distill(recorded_balance_run()[:-1], ctx())
    bad = recorded_balance_run()
    bad[-1].record.args = {"outputs": {"savings_balance": "nope"}}
    with pytest.raises(DistillError, match="unknown ref"):
        distill(bad, ctx())


def test_title_fallback_when_every_static_text_carries_a_param_value(tmp_path) -> None:  # noqa: ANN001
    from anton.distill.distiller import title_landmark
    from anton.schema.artifact import TitleMatches

    before = search_page("204817")
    after = SemanticSnapshot(url="http://x/member/204817/subaccount/new", title="New sub-account — Member Servicing Console", frame_count=1, elements=[
        cell("h1", "New sub-account for Alpha Sample (204817)", 10, 10, role="text", ct="span"), cell("h2", "Signed in as ops_user", 900, 5, role="text", ct="span"),
        cell("c1", "Nickname", 20, 60), cell("i1", "", 130, 60, role="textbox", name="Nickname", ct="input"),
    ])
    assert landmark_text(before, after, ["204817"]) is None  # the heading quotes the param value
    assert title_landmark(before, after, ["204817"]) == "New sub-account"  # the brand segment is not new
    assert title_landmark(after, after, []) is None
    turns = recorded_balance_run()
    turns[-2].after = after
    turns[-1].before = after
    turns[-2].after_path = "/member/204817/subaccount/new"
    turns[-1].before_path = "/member/204817/subaccount/new"
    turns[-1].record.args = {"outputs": {"savings_balance": "i1"}}
    art = distill(turns, ctx(read_values=[]))
    assert any(isinstance(p, TitleMatches) and p.regex == "New sub-account" for p in art.steps[-1].postcondition.all_of)


def test_redistill_reproduces_the_same_contract(tmp_path) -> None:  # noqa: ANN001
    import json

    from anton.distill.distiller import load_recorded_run, redistill
    from anton.schema.artifact import content_hash

    run = tmp_path / "run"
    (run / "digests").mkdir(parents=True)
    turns = recorded_balance_run()
    with (run / "turns.jsonl").open("w") as f:
        for i, t in enumerate(turns, start=1):
            t.record.digest_path = f"digests/turn-{i:02d}.json"
            (run / t.record.digest_path).write_text(json.dumps({"path": t.before_path, "url": t.before.url, "title": t.before.title, "viewport": list(VIEW), "elements": [], "snapshot": t.before.model_dump(mode="json")}))
            f.write(t.record.model_dump_json() + "\n")
    (run / "discovery.json").write_text(json.dumps({"run_id": "20260828T120000Z-abc123", "params": {"member_id": "204817"}, "entry_url": "http://localhost:8000/search", "route_prefix": "", "model": "claude-sonnet-5", "read_values": ["$19,823.28"]}))
    loaded, meta = load_recorded_run(run)
    assert len(loaded) == 4 and loaded[0].after is loaded[1].before and loaded[-1].after is None and meta["run_id"] == "20260828T120000Z-abc123"
    a = redistill(run, spec(), make_profile(), "profiles/servicing.yaml")
    b = redistill(run, spec(), make_profile(), "profiles/servicing.yaml")
    assert content_hash(a) == content_hash(b) and a.provenance.discovery_run_id == "20260828T120000Z-abc123"  # same run ⇒ same contract
    direct = distill(recorded_balance_run(), ctx())
    assert [s.id for s in a.steps] == [s.id for s in direct.steps]


def test_failed_act_and_denied_act_are_never_steps() -> None:
    turns = recorded_balance_run()
    turns[0].record.gate_decision = "Allow:SAFE;act_failed"
    art = distill(turns, ctx())
    assert [s.id for s in art.steps] == ["click_search"]
    turns = recorded_balance_run()
    turns[0].record.gate_decision = "Deny:SECRET_TARGET"
    assert [s.id for s in distill(turns, ctx()).steps] == ["click_search"]


def test_navigate_values_are_origin_relative_and_created_at_is_run_derived() -> None:
    from anton.distill.distiller import relative_url

    assert relative_url("http://127.0.0.1:8000/member/204817?x=1") == "/member/204817?x=1" and relative_url("/search") == "/search"
    art = distill(recorded_balance_run(), ctx(run_id="20260828T184218Z-e03847"))
    assert art.provenance.created_at.isoformat() == "2026-08-28T18:42:18+00:00"


def test_describe_never_fabricates_a_label_from_an_attribute_name() -> None:
    s = search_page()
    s.elements.append(cell("i9", "", 400, 300, role="textbox", name="Search members", ct="input"))  # aria-named, no visible label
    d = describe(s, next(e for e in s.elements if e.ref == "i9"))
    assert d.label_context is None
    lad = ladder_for(d, VIEW, PARAMS)
    assert not any(isinstance(c, LabelRelativeLocator) for c in lad.candidates)
