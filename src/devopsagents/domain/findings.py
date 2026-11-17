"""Findings produced by agents and their stable fingerprints."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}[self.value]


def fingerprint(kind: str, rule: str, path: str, code: str) -> str:
    """Stable across line shifts: hashes whitespace-normalised code, not the line number."""
    norm = re.sub(r"\s+", " ", code).strip()
    return hashlib.sha256(f"{kind}|{rule}|{path}|{norm}".encode()).hexdigest()[:16]


class Finding(BaseModel):
    kind: str  # "review" | "security" | "secret"
    rule: str
    severity: Severity
    path: str
    line: int = Field(ge=1)
    title: str
    detail: str = ""
    suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    code: str = ""

    @property
    def fp(self) -> str:
        return fingerprint(self.kind, self.rule, self.path, self.code or self.title)
