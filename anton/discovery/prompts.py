"""What the model is told (SPEC §10.1): the system prompt and the five tool definitions.

Kept in one place so the prompt can be reviewed like any other contract. The prompt never carries
a credential or a sensitive param value; refs are turn-scoped; one tool call per turn.
"""

from __future__ import annotations

from typing import Any

from anton.schema.spec import CapabilitySpec

__all__ = ["TOOLS", "system_prompt"]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "act",
        "description": "Perform ONE UI action. CLICK/FILL/SELECT/PRESS on a digest element need its ref; NAVIGATE (value = URL) and a global PRESS (value = key name) use ref = null. FILL/SELECT values may be a literal, \"$param:<name>\" for a declared param, or \"$secret:<name>\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": ["string", "null"], "description": "element ref from THIS turn's digest, e.g. \"e12\"; null only for NAVIGATE / global PRESS"},
                "action": {"type": "string", "enum": ["CLICK", "FILL", "SELECT", "PRESS", "NAVIGATE"]},
                "value": {"type": ["string", "null"], "description": "text to type / option label to select / key name / URL"},
                "rationale": {"type": "string", "description": "one sentence: why this action moves toward the goal"},
            },
            "required": ["ref", "action", "rationale"],
        },
    },
    {
        "name": "read",
        "description": "Return the (redacted) text of one element of THIS turn's digest. Use it to confirm a value before finishing.",
        "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "rationale": {"type": "string"}}, "required": ["ref", "rationale"]},
    },
    {
        "name": "finish",
        "description": "Declare the goal reached. For EVERY declared output give the ref of the element whose text IS that value (for a table: the cell in the right row AND column). The harness reads the value itself — never transcribe it.",
        "input_schema": {
            "type": "object",
            "properties": {"outputs": {"type": "object", "additionalProperties": {"type": "string"}, "description": "{output name: ref}"}, "rationale": {"type": "string"}},
            "required": ["outputs", "rationale"],
        },
    },
    {
        "name": "give_up",
        "description": "Stop: the goal cannot be reached from here (explain why).",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "ask_human",
        "description": "Ask a human operator to take over (only useful when the system prompt says a human is available).",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
]



def system_prompt(goal: str, spec: CapabilitySpec, params: dict[str, str], *, human_available: bool) -> str:
    parts = []
    for p in spec.inputs:
        shown_value = f'(sensitive; type "$param:{p.name}")' if p.sensitive else str(params.get(p.name, ""))
        parts.append(f"{p.name}={shown_value} — {p.description}")
    shown = ", ".join(parts)
    outs = "; ".join(f"{d.name} ({d.type.value}): {d.description}" for d in spec.outputs)
    human = "A human operator IS available via ask_human." if human_available else "NO human is available: ask_human is treated as give_up."
    return (
        "You operate a legacy back-office web console through a strict harness. You are ALREADY authenticated and "
        f"standing on the entry page.\nGOAL: {goal}\n"
        f"Declared params: {shown}\nDeclared outputs you must cite in finish: {outs}\n"
        "Each turn you receive a digest of the visible elements (refs like e12 are valid ONLY for that turn) and a masked "
        "screenshot. Call exactly ONE tool per turn. Type \"$param:<name>\" as the FILL value for a declared param. "
        "Use read to confirm a cell's text before finishing. When every declared output's value is visible, call finish "
        f"citing the ref of the element whose text IS the value. Never transcribe values. {human} "
        "If the goal is impossible, call give_up with the reason."
    )
