"""Filter and pack diff files into token-budgeted chunks; report exactly what was covered."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from devopsagents.domain.diff import FileDiff, Hunk


def est_tokens(text: str) -> int:
    return len(text) // 4 + 1


def is_ignored(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return True
        # "**/x" should also match a top-level "x"
        if pat.startswith("**/") and fnmatch.fnmatch(path, pat[3:]):
            return True
        if pat.endswith("/**") and path.startswith(pat[:-3].lstrip("*/") + "/"):
            return True
    return False


@dataclass
class Coverage:
    total_files: int = 0
    reviewed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # path -> reason
    truncated: list[str] = field(default_factory=list)

    def summary(self) -> str:
        s = f"Reviewed {len(self.reviewed)} of {self.total_files} changed file(s)."
        if self.skipped:
            s += f" Skipped {len(self.skipped)} (ignored/binary/deleted/over budget)."
        if self.truncated:
            s += f" {len(self.truncated)} file(s) were partially reviewed (size limit)."
        return s


@dataclass
class Chunk:
    files: list[str]
    text: str


def build_chunks(
    files: list[FileDiff], ignore: list[str], chunk_tokens: int = 6000, max_chunks: int = 12
) -> tuple[list[Chunk], Coverage]:
    cov = Coverage(total_files=len(files))
    eligible: list[tuple[str, list[str]]] = []  # (path, rendered pieces <= budget)

    for f in files:
        if f.is_binary:
            cov.skipped[f.path] = "binary"
        elif f.is_deleted:
            cov.skipped[f.path] = "deleted"
        elif is_ignored(f.path, ignore):
            cov.skipped[f.path] = "ignored by config"
        elif not f.hunks:
            cov.skipped[f.path] = "no textual changes"
        else:
            pieces, was_truncated = _split_file(f, chunk_tokens)
            if was_truncated:
                cov.truncated.append(f.path)
            eligible.append((f.path, pieces))

    chunks: list[Chunk] = []
    cur_files: list[str] = []
    cur_parts: list[str] = []
    cur_tok = 0

    def flush() -> None:
        nonlocal cur_files, cur_parts, cur_tok
        if cur_parts:
            chunks.append(Chunk(files=list(dict.fromkeys(cur_files)), text="\n\n".join(cur_parts)))
        cur_files, cur_parts, cur_tok = [], [], 0

    for path, pieces in eligible:
        for piece in pieces:
            t = est_tokens(piece)
            if cur_tok + t > chunk_tokens:
                flush()
            if len(chunks) >= max_chunks:
                cov.skipped[path] = "over review budget"
                break
            cur_files.append(path)
            cur_parts.append(piece)
            cur_tok += t
    if len(chunks) < max_chunks:
        flush()
    else:
        cur_parts = []

    reviewed = {p for c in chunks for p in c.files}
    cov.reviewed = sorted(reviewed)
    for path, _ in eligible:
        if path not in reviewed:
            cov.skipped.setdefault(path, "over review budget")
    return chunks, cov


def _split_file(f: FileDiff, budget: int) -> tuple[list[str], bool]:
    """Split by hunk so every piece fits the budget; oversized single hunks are truncated."""
    pieces: list[str] = []
    truncated = False
    cur: list[Hunk] = []
    cur_tok = 0
    head = f"=== {f.path} ==="
    for h in f.hunks:
        text = h.render()
        if est_tokens(text) > budget:
            text = text[: budget * 4] + "\n[... hunk truncated ...]"
            truncated = True
        t = est_tokens(text)
        if cur and cur_tok + t > budget:
            pieces.append(head + "\n" + "\n".join(cur_txt for cur_txt in _renders(cur)))
            cur, cur_tok = [], 0
        cur.append(_StubHunk(text))  # type: ignore[arg-type]
        cur_tok += t
    if cur:
        pieces.append(head + "\n" + "\n".join(_renders(cur)))
    return pieces, truncated


class _StubHunk:
    """Pre-rendered hunk text (lets us truncate without mutating the parsed diff)."""

    def __init__(self, text: str) -> None:
        self._t = text

    def render(self) -> str:
        return self._t


def _renders(hs: list[Hunk]) -> list[str]:
    return [h.render() for h in hs]
