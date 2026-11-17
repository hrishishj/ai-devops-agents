"""Grounding: a comment may only be posted on a line that exists in the diff and matches the
code the model quoted. Everything else is dropped (or demoted to a summary note) — this is what
prevents GitHub 422s and hallucinated line references."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from devopsagents.domain.diff import FileDiff
from devopsagents.domain.findings import Severity


class Candidate(BaseModel):
    path: str
    line: int = Field(ge=1)
    quote: str = Field(default="", description="exact code from that line that the issue refers to")
    severity: Severity
    category: str = "bug"  # bug | security | perf | correctness | maintainability
    title: str
    detail: str
    suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CandidateList(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)


@dataclass
class GroundingResult:
    grounded: list[Candidate]
    dropped: list[tuple[Candidate, str]]


def _norm(s: str) -> str:
    return " ".join(s.split())


def ground(candidates: list[Candidate], files: list[FileDiff], snap: int = 2) -> GroundingResult:
    by_path = {f.path: f for f in files}
    grounded: list[Candidate] = []
    dropped: list[tuple[Candidate, str]] = []

    for c in candidates:
        f = by_path.get(c.path)
        if f is None:
            dropped.append((c, "path not in diff"))
            continue
        ok = f.commentable_lines
        added = f.added_lines
        line = c.line

        if line not in ok:
            near = [n for n in sorted(ok, key=lambda n: abs(n - line)) if abs(n - line) <= snap]
            if not near:
                dropped.append((c, "line not in diff"))
                continue
            line = near[0]

        if c.quote:
            q = _norm(c.quote)
            text = _norm(added.get(line, "")) or _norm(_context_text(f, line))
            if q not in text:
                # the quote may point to a different added line nearby
                relocated = next((n for n, t in added.items() if abs(n - line) <= 5 and q in _norm(t)), None)
                if relocated is None:
                    dropped.append((c, "quote does not match diff"))
                    continue
                line = relocated

        grounded.append(c.model_copy(update={"line": line}))
    return GroundingResult(grounded, dropped)


def _context_text(f: FileDiff, line: int) -> str:
    for h in f.hunks:
        for k, no, t in h.lines:
            if no == line and k == " ":
                return t
    return ""
