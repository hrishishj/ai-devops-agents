"""Findings produced by agents and their stable fingerprints."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum


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
