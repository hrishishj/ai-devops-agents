"""Remediation action requests and policy decisions (pure types)."""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field


class RiskTier(IntEnum):
    DIAGNOSTIC = 0  # read-only data gathering
    REVERSIBLE = 1  # restart, scale +1 — low blast radius, reversible
    STATEFUL = 2  # failover, rotate, drain — needs human approval
    DESTRUCTIVE = 3  # never automated


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ActionRequest(BaseModel):
    runbook: str
    params: dict[str, object] = Field(default_factory=dict)
    environment: str
    target: str  # resource the action touches (cooldown / rate-limit scope)
    incident_id: str
    severity: str = "unknown"
    rationale: str = ""
    model_confidence: float = 0.0  # advisory only


class PolicyResult(BaseModel):
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
