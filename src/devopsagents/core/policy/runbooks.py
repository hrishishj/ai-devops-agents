"""Runbook registry: runbooks/<name>/runbook.yaml with typed params, tier and safety metadata."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from devopsagents.domain.actions import RiskTier

_REF = re.compile(r"\{(\w+)\}")
_WHOLE_REF = re.compile(r"\{(\w+)\}")


class VerificationSpec(BaseModel):
    description: str
    wait_seconds: int = Field(default=120, ge=0, le=3600)


class RunbookSpec(BaseModel):
    name: str
    description: str
    tier: RiskTier
    automation_runbook: str  # name of the Azure Automation runbook to start
    params_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    allowed_environments: list[str] = Field(default_factory=lambda: ["dev", "staging", "prod"])
    cooldown_seconds: int = Field(default=900, ge=0)
    max_per_hour: int = Field(default=3, ge=1)
    verification: VerificationSpec
    rollback: str | None = None  # name of another registered runbook
    # Maps the rollback runbook's parameters from this runbook's: "{name}" copies a parameter (type kept),
    # other strings are formatted, anything else is used literally.
    rollback_params: dict[str, Any] = Field(default_factory=dict)
    # Undo automatically if recovery is verified as FAILED. Off by default: undoing a fix that merely did
    # not help (e.g. scale-out) can make an incident worse, so opt in only where undo is clearly safer.
    auto_rollback: bool = False


class RunbookRegistry:
    def __init__(self, specs: dict[str, RunbookSpec]) -> None:
        self._specs = specs

    @classmethod
    def load(cls, root: Path | str) -> RunbookRegistry:
        specs: dict[str, RunbookSpec] = {}
        for f in sorted(Path(root).glob("*/runbook.yaml")):
            spec = RunbookSpec.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))
            if spec.name != f.parent.name:
                raise ValueError(f"runbook name {spec.name!r} must match folder {f.parent.name!r}")
            Draft202012Validator.check_schema(spec.params_schema)
            specs[spec.name] = spec
        for s in specs.values():
            if s.rollback and s.rollback not in specs:
                raise ValueError(f"{s.name}: unknown rollback runbook {s.rollback!r}")
            if (s.rollback_params or s.auto_rollback) and not (s.rollback and s.rollback_params):
                raise ValueError(
                    f"{s.name}: auto_rollback/rollback_params need both rollback and rollback_params"
                )
            cls._check_rollback_mapping(s, specs)
        return cls(specs)

    @staticmethod
    def _check_rollback_mapping(s: RunbookSpec, specs: dict[str, RunbookSpec]) -> None:
        if not s.rollback_params or not s.rollback:
            return
        own = set((s.params_schema.get("properties") or {}).keys())
        target = set((specs[s.rollback].params_schema.get("properties") or {}).keys())
        for key, tmpl in s.rollback_params.items():
            if target and key not in target:
                raise ValueError(f"{s.name}: rollback param {key!r} is not a parameter of {s.rollback}")
            if isinstance(tmpl, str) and own:
                for ref in _REF.findall(tmpl):
                    if ref not in own:
                        raise ValueError(
                            f"{s.name}: rollback param {key!r} references unknown parameter {ref!r}"
                        )

    def build_rollback_params(self, name: str, params: dict[str, object]) -> dict[str, object] | None:
        """Rollback runbook parameters, or None if undefined or a referenced value is missing."""
        spec = self._specs[name]
        if not spec.rollback or not spec.rollback_params:
            return None
        out: dict[str, object] = {}
        for key, tmpl in spec.rollback_params.items():
            if isinstance(tmpl, str):
                whole = _WHOLE_REF.fullmatch(tmpl)
                if whole:
                    if whole.group(1) not in params:
                        return None
                    out[key] = params[whole.group(1)]  # copy as-is so integers stay integers
                    continue
                try:
                    out[key] = _REF.sub(lambda m: str(params[m.group(1)]), tmpl)
                except KeyError:
                    return None
            else:
                out[key] = tmpl
        return out

    def get(self, name: str) -> RunbookSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def validate_params(self, name: str, params: dict[str, object]) -> list[str]:
        spec = self._specs[name]
        v = Draft202012Validator(spec.params_schema)
        return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in v.iter_errors(params)]
