"""Versioned prompt registry: prompts/<agent>/<name>.v<N>.md, reviewed like code.

The resolved id (e.g. ``review/discover.v2``) is stored on every run for traceability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FILE_RE = re.compile(r"^(?P<name>[a-z0-9_-]+)\.v(?P<ver>\d+)\.md$")


@dataclass(frozen=True)
class Prompt:
    agent: str
    name: str
    version: int
    text: str

    @property
    def id(self) -> str:
        return f"{self.agent}/{self.name}.v{self.version}"


class PromptNotFound(LookupError):
    pass


class PromptRegistry:
    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def get(self, agent: str, name: str, version: int | None = None) -> Prompt:
        folder = self._root / agent
        found: dict[int, Path] = {}
        if folder.is_dir():
            for p in folder.iterdir():
                m = _FILE_RE.match(p.name)
                if m and m.group("name") == name:
                    found[int(m.group("ver"))] = p
        if not found:
            raise PromptNotFound(f"{agent}/{name}")
        ver = version if version is not None else max(found)
        if ver not in found:
            raise PromptNotFound(f"{agent}/{name}.v{ver}")
        return Prompt(agent, name, ver, found[ver].read_text(encoding="utf-8").strip())
