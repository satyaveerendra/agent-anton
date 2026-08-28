"""SPEC §16 "Redaction" (unit half): values scrubbed everywhere, anchors and control names kept."""

from __future__ import annotations

from anton.policy.gate import PolicyConfig
from anton.policy.redact import MASK, RawElement, Redactor
from anton.schema.snapshot import BBox

SSN = "900-37-2769"


def raw(ref: str, role: str, text: str, x: float, y: float, w: float = 100, h: float = 20, *, name: str | None = None,
        value: str | None = None, label: str | None = None, interactive: bool = False, control_type: str = "td",
        frame: list[str] | None = None) -> RawElement:
    return RawElement(
        ref=ref, role=role, name=text if name is None else name, text=text, control_type=control_type, frame_path=frame or [],
        bbox=BBox(x=x, y=y, w=w, h=h), enabled=True, visible=True, value=value, checked=None, label_context=label, interactive=interactive,
    )


def make_redactor(policy: PolicyConfig, **kw) -> Redactor:
    return Redactor(policy.redact_field_patterns, policy.redact_value_patterns, **kw)


def test_nameless_ssn_cell_is_scrubbed_but_the_label_cell_is_kept(policy: PolicyConfig) -> None:
    r = make_redactor(policy)
    pane = ["iframe[name=detail]"]
    label = raw("e1", "cell", "SSN", 0, 100, frame=pane)
    value = raw("e2", "cell", SSN, 110, 100, frame=pane)
    dob_label = raw("e3", "cell", "Date of Birth", 0, 80, frame=pane)
    dob_value = raw("e4", "cell", "2002-05-02", 110, 80, frame=pane)
    # Rule (3) by mask region: the SSN cell's box lies inside the resolved mask ladder's box.
    out = r.scrub_elements([label, value, dob_label, dob_value], mask_regions=[value.bbox, dob_value.bbox])
    by = {e.ref: e for e in out}
    assert by["e1"].text == "SSN" and by["e1"].name == "SSN"  # anchor, never scrubbed
    assert by["e2"].text == MASK and by["e2"].name == MASK
    assert by["e3"].text == "Date of Birth"
    assert by["e4"].text == MASK
    assert SSN not in "".join(e.text + e.name for e in out)


def test_label_context_rule_and_form_controls_keep_their_name(policy: PolicyConfig) -> None:
    r = make_redactor(policy)
    pw_label = raw("e1", "cell", "Password", 0, 0)
    pw_input = raw("e2", "textbox", "", 110, 0, name="Password", value="demo-pass-1", label="Password", interactive=True, control_type="input")
    user_input = raw("e3", "textbox", "", 110, 30, name="Username", value="ops_user", label="Username", interactive=True, control_type="input")
    out = {e.ref: e for e in r.scrub_elements([pw_label, pw_input, user_input], mask_regions=[])}
    assert out["e1"].text == "Password"  # the label is an anchor
    assert out["e2"].name == "Password" and out["e2"].value == MASK  # control keeps its name; the value goes
    assert out["e3"].name == "Username" and out["e3"].value == "ops_user"


def test_account_number_label_and_value(policy: PolicyConfig) -> None:
    r = make_redactor(policy)
    label = raw("e1", "cell", "Account Number", 0, 0)
    value = raw("e2", "cell", "7204817001", 110, 0)
    header = raw("e3", "columnheader", "Account No.", 0, 200)
    out = {e.ref: e for e in r.scrub_elements([label, value, header], mask_regions=[value.bbox])}
    assert out["e1"].text == "Account Number" and out["e3"].text == "Account No."
    assert out["e2"].text == MASK


def test_free_text_ssn_and_known_values_are_scrubbed_everywhere(policy: PolicyConfig) -> None:
    r = make_redactor(policy, known_values={"demo-pass-1"})
    assert r.scrub_text(f"member ssn is {SSN} ok") == f"member ssn is {MASK} ok"
    assert r.scrub_text("typed demo-pass-1 twice demo-pass-1") == f"typed {MASK} twice {MASK}"
    r.add_sensitive_value("7204817001")
    r.add_sensitive_value("x")  # too short to be worth hiding: must not wipe every string
    nested = {"a": [f"acct 7204817001 / {SSN}", {"b": "plain"}], "c": ("x", "demo-pass-1")}
    assert r.scrub(nested) == {"a": [f"acct {MASK} / {MASK}", {"b": "plain"}], "c": ("x", MASK)}
    assert r.scrub("no secrets here") == "no secrets here"


def test_scrub_handles_pydantic_models(policy: PolicyConfig) -> None:
    from anton.schema.snapshot import SnapshotElement

    r = make_redactor(policy)
    el = SnapshotElement(ref="e1", role="text", name=SSN, text=f"SSN {SSN}", control_type="td", frame_path=[], bbox=BBox(x=0, y=0, w=1, h=1), enabled=True, visible=True)
    dumped = r.scrub(el)
    assert dumped["name"] == MASK and dumped["text"] == f"SSN {MASK}"


def test_static_value_cells_are_scrubbed_by_label_context_without_any_mask(policy: PolicyConfig) -> None:
    """Rule (3)'s nearest-label branch must not depend on a mask ladder resolving."""
    r = make_redactor(policy)
    dob_label = raw("e1", "cell", "Date of Birth", 0, 80)
    dob_value = raw("e2", "cell", "2002-05-02", 110, 80, label="Date of Birth")
    hdr = raw("e3", "cell", "Account No.", 120, 200)
    acct = raw("e4", "cell", "220481701", 120, 225, label="Account No.")
    typ = raw("e5", "cell", "Savings", 0, 225, label="Type")
    out = {e.ref: e for e in r.scrub_elements([dob_label, dob_value, hdr, acct, typ], mask_regions=[])}
    assert out["e1"].text == "Date of Birth" and out["e3"].text == "Account No."
    assert out["e2"].text == MASK and out["e4"].text == MASK
    assert out["e5"].text == "Savings"


def test_mask_region_wins_over_the_anchor_exemption_and_anchors_are_short(policy: PolicyConfig) -> None:
    r = make_redactor(policy)
    note = raw("e1", "cell", "my ssn is here honestly", 110, 100)
    out = {e.ref: e for e in r.scrub_elements([note], mask_regions=[note.bbox])}
    assert out["e1"].text == MASK
    long_text = raw("e2", "text", "Please keep your social security card in a safe place at all times", 0, 0, w=600)
    out = {e.ref: e for e in r.scrub_elements([long_text], mask_regions=[])}
    assert out["e2"].text == long_text.text  # not an anchor, not labelled, not masked: untouched
