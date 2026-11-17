"""Runbook registry: runbooks/<name>/runbook.yaml with typed params, tier and safety metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from devopsagents.domain.actions import RiskTier


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
        return cls(specs)

    def get(self, name: str) -> RunbookSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def validate_params(self, name: str, params: dict[str, object]) -> list[str]:
        spec = self._specs[name]
        v = Draft202012Validator(spec.params_schema)
        return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in v.iter_errors(params)]
