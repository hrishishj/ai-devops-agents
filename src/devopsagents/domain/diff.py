"""Pure unified-diff parser.

Produces, per file, the set of new-side line numbers that GitHub allows review comments on
(added + context lines inside hunks) so comments can be *grounded* before posting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    new_start: int
    header: str
    lines: list[tuple[str, int | None, str]] = field(default_factory=list)
    # (kind, new_line_no, text) where kind in {"+", " ", "-"}; new_line_no None for "-"

    def render(self) -> str:
        out = [self.header]
        for kind, no, text in self.lines:
            prefix = f"{no:>5}" if no is not None else "     "
            out.append(f"{prefix} {kind}{text}")
        return "\n".join(out)


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def commentable_lines(self) -> set[int]:
        return {no for h in self.hunks for k, no, _ in h.lines if k in "+ " and no is not None}

    @property
    def added_lines(self) -> dict[int, str]:
        return {no: t for h in self.hunks for k, no, t in h.lines if k == "+" and no is not None}

    @property
    def changed_line_count(self) -> int:
        return sum(1 for h in self.hunks for k, _, _ in h.lines if k in "+-")

    def render(self) -> str:
        return f"=== {self.path} ===\n" + "\n".join(h.render() for h in self.hunks)


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    new_no = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            cur, hunk = None, None
            m = re.match(r"diff --git a/(.*) b/(.*)$", raw)
            if m:
                cur = FileDiff(path=m.group(2), old_path=m.group(1))
                files.append(cur)
            continue
        if cur is None:
            continue
        if hunk is None or raw.startswith("@@"):
            if raw.startswith("new file mode"):
                cur.is_new = True
            elif raw.startswith("deleted file mode"):
                cur.is_deleted = True
            elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
                cur.is_binary = True
            elif raw.startswith("+++ "):
                target = raw[4:].strip()
                if target == "/dev/null":
                    cur.is_deleted = True
                elif target.startswith("b/"):
                    cur.path = target[2:]
            elif raw.startswith("@@"):
                m = _HUNK_RE.match(raw)
                if m:
                    new_no = int(m.group(3))
                    hunk = Hunk(new_start=new_no, header=raw)
                    cur.hunks.append(hunk)
            continue
        # inside a hunk
        if raw.startswith("+"):
            hunk.lines.append(("+", new_no, raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            hunk.lines.append(("-", None, raw[1:]))
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            hunk.lines.append((" ", new_no, raw[1:] if raw.startswith(" ") else raw))
            new_no += 1
    return files
