"""Deterministic policy engine. The LLM proposes; this module decides.

Model confidence is *advisory*: it can only make an outcome stricter, never authorise an action.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from devopsagents.core.jobs.store import JobStore
from devopsagents.core.policy.runbooks import RunbookRegistry
from devopsagents.domain.actions import ActionRequest, Decision, PolicyResult, RiskTier


class FreezeWindow(BaseModel):
    start: datetime
    end: datetime
    reason: str = "change freeze"


class PolicyConfig(BaseModel):
    kill_switch: bool = False
    # Environments where tier-1 (reversible) actions may run without a human.
    auto_environments: list[str] = Field(default_factory=lambda: ["dev", "staging"])
    # Flip to include "prod" only after shadow-mode precision is proven.
    min_confidence_for_auto: float = Field(default=0.8, ge=0, le=1)
    require_evidence: bool = True
    freeze_windows: list[FreezeWindow] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str) -> PolicyConfig:
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {})


class PolicyEngine:
    def __init__(self, config: PolicyConfig, registry: RunbookRegistry, store: JobStore) -> None:
        self._cfg = config
        self._reg = registry
        self._store = store

    def evaluate(
        self, req: ActionRequest, *, evidence: list[str], now: datetime | None = None
    ) -> PolicyResult:
        now = now or datetime.fromtimestamp(self._store.now(), UTC)
        deny = self._deny

        if self._cfg.kill_switch:
            return deny("global kill switch is on")
        spec = self._reg.get(req.runbook)
        if spec is None:
            return deny(f"unknown runbook {req.runbook!r} (not in registry)")
        errors = self._reg.validate_params(req.runbook, req.params)
        if errors:
            return deny("invalid parameters: " + "; ".join(errors))
        if req.environment not in spec.allowed_environments:
            return deny(f"runbook not allowed in environment {req.environment!r}")
        if spec.tier >= RiskTier.DESTRUCTIVE:
            return deny("destructive actions are never automated")

        if spec.tier == RiskTier.DIAGNOSTIC:
            return PolicyResult(decision=Decision.ALLOW, reasons=["diagnostic (read-only)"])

        if self._cfg.require_evidence and not evidence:
            return deny("no supporting evidence collected")

        now_ts = now.timestamp()
        recent_cd = self._store.count_audit("action.executed", req.target, now_ts - spec.cooldown_seconds)
        if recent_cd:
            return deny(f"cooldown active ({spec.cooldown_seconds}s) for {req.target}")
        recent_hr = self._store.count_audit("action.executed", req.target, now_ts - 3600)
        if recent_hr >= spec.max_per_hour:
            return deny(f"rate limit reached ({spec.max_per_hour}/h) for {req.target}")

        reasons: list[str] = []
        needs_human = False

        if spec.tier >= RiskTier.STATEFUL:
            needs_human = True
            reasons.append("stateful action requires human approval")
        elif req.environment not in self._cfg.auto_environments:
            needs_human = True
            reasons.append(f"auto-execution not enabled for {req.environment!r}")

        for w in self._cfg.freeze_windows:
            if w.start <= now <= w.end:
                needs_human = True
                reasons.append(f"change freeze: {w.reason}")

        if req.model_confidence < self._cfg.min_confidence_for_auto:
            needs_human = True
            reasons.append(
                f"model confidence {req.model_confidence:.2f} < {self._cfg.min_confidence_for_auto}"
            )

        if needs_human:
            return PolicyResult(decision=Decision.REQUIRE_APPROVAL, reasons=reasons)
        return PolicyResult(decision=Decision.ALLOW, reasons=["tier-1 reversible, gates passed"])

    @staticmethod
    def _deny(reason: str) -> PolicyResult:
        return PolicyResult(decision=Decision.DENY, reasons=[reason])
