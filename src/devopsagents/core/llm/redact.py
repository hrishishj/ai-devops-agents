"""Secret detection/redaction. Used before text leaves the service AND by the secrets scanner."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretPattern:
    rule: str
    description: str
    regex: re.Pattern[str]


SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern("aws-access-key", "AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    SecretPattern(
        "github-token",
        "GitHub token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
    ),
    SecretPattern(
        "private-key",
        "Private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    SecretPattern("slack-token", "Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    SecretPattern(
        "azure-storage-key", "Azure storage/connection key", re.compile(r"AccountKey=[A-Za-z0-9+/=]{40,}")
    ),
    SecretPattern("openai-key", "API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    SecretPattern(
        "hardcoded-credential",
        "Hard-coded credential assignment",
        re.compile(
            r"""(?ix)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b
            \s*[:=]\s*['"](?!\s*['"])(?!.*(?:example|changeme|placeholder|your[_-]|xxx|<|\$\{|\{\{))
            [^'"\s]{8,}['"]"""
        ),
    ),
)

REDACTED = "[REDACTED]"


def find_secrets(text: str) -> list[tuple[SecretPattern, int]]:
    """Return (pattern, 1-based line) for every match."""
    hits: list[tuple[SecretPattern, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for p in SECRET_PATTERNS:
            if p.regex.search(line):
                hits.append((p, lineno))
    return hits


def redact(text: str) -> str:
    """Replace every detected secret value with a marker (whole match; conservative)."""
    out = text
    for p in SECRET_PATTERNS:
        out = p.regex.sub(REDACTED, out)
    return out
