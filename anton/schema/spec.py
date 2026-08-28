"""The declared capability interface for ``anton discover --spec`` (SPEC §10.1, §13).

``inputs`` are full ``ParamSpec``s; ``outputs`` are everything in ``OutputSpec`` except
``extraction`` — the distiller synthesises only the extraction. Copied verbatim into the artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import Field, ValidationError, model_validator

from anton.schema.artifact import CAPABILITY_ID, OutputSpec, OutputType, ParamSpec, StrictModel
from anton.schema.loader import LoaderError, resolve_repo_path

__all__ = ["OutputDecl", "CapabilitySpec", "load_spec"]


class OutputDecl(StrictModel):
    name: str
    type: OutputType
    description: str
    sensitive: bool = False
    enum_values: list[str] | None = None

    @model_validator(mode="after")
    def _enum_needs_values(self) -> OutputDecl:
        if self.type is OutputType.ENUM and not self.enum_values:
            raise ValueError(f"output {self.name!r}: ENUM requires enum_values")
        return self

    def to_output_spec(self, extraction) -> OutputSpec:  # noqa: ANN001 - ExtractionSpec
        return OutputSpec(name=self.name, type=self.type, description=self.description, sensitive=self.sensitive, enum_values=self.enum_values, extraction=extraction)


class CapabilitySpec(StrictModel):
    capability_id: str = Field(pattern=CAPABILITY_ID.pattern)
    name: str
    description: str
    inputs: list[ParamSpec]
    outputs: list[OutputDecl] = Field(min_length=1)
    #: Optional meanings for codes the profile's detectors emit; missing ones get a generated meaning.
    business_outcomes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique(self) -> CapabilitySpec:
        names = [p.name for p in self.inputs]
        if len(set(names)) != len(names):
            raise ValueError("input names must be unique")
        onames = [o.name for o in self.outputs]
        if len(set(onames)) != len(onames):
            raise ValueError("output names must be unique")
        for code in self.business_outcomes:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code):
                raise ValueError(f"outcome code {code!r} must be UPPER_SNAKE")
        return self


def load_spec(path: str | Path) -> CapabilitySpec:
    p = resolve_repo_path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise LoaderError(f"file not found: {p}") from e
    except yaml.YAMLError as e:
        raise LoaderError(f"{p}: invalid YAML ({e})") from e
    if not isinstance(data, dict):
        raise LoaderError(f"{p}: expected a mapping at top level")
    try:
        return CapabilitySpec.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise LoaderError(f"{p}: {loc}: {first.get('msg', 'invalid')}") from e
