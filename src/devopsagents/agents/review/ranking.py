"""Dedupe, filter, rank and cap review candidates."""

from __future__ import annotations

from devopsagents.agents.review.grounding import Candidate
from devopsagents.domain.findings import fingerprint


def candidate_fp(c: Candidate) -> str:
    return fingerprint("review", c.category, c.path, c.quote or c.title)


def rank(
    candidates: list[Candidate],
    *,
    min_confidence: float,
    max_comments: int,
    suppressed: set[str] | None = None,
) -> tuple[list[Candidate], list[Candidate]]:
    """Returns (to_post, overflow). `suppressed` = fingerprints already dismissed by humans."""
    suppressed = suppressed or set()
    seen: set[tuple[str, int, str]] = set()
    kept: list[Candidate] = []
    for c in candidates:
        if c.confidence < min_confidence or candidate_fp(c) in suppressed:
            continue
        key = (c.path, c.line, c.title.lower().strip())
        if key in seen:
            continue
        seen.add(key)
        kept.append(c)
    kept.sort(key=lambda c: (c.severity.rank * c.confidence, c.severity.rank), reverse=True)
    return kept[:max_comments], kept[max_comments:]
