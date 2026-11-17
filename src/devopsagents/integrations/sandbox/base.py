"""Sandbox port for running untrusted code (generated tests against PR code).

SECURITY: PR code is attacker-controlled. It must NEVER execute in the worker process, which holds a
managed identity. Adapters must run it in an isolated environment with no identity and no secrets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class SandboxUnavailable(Exception):
    """The sandbox could not run the job (missing endpoint, no pytest, transport error)."""


class Sandbox(Protocol):
    def run_pytest(self, files: dict[str, str], target: str, timeout_seconds: int = 60) -> SandboxResult:
        """Write `files` (relative path -> content) into a fresh directory and run
        `python -m pytest -q -rA --tb=short -p no:cacheprovider <target>` there."""
        ...


_RESULT_RE = re.compile(r"^(PASSED|FAILED|ERROR)\s+\S+?::(\w+)", re.MULTILINE)


def parse_pytest_outcomes(stdout: str) -> dict[str, str]:
    """Map test function name -> PASSED|FAILED|ERROR from `pytest -rA` output."""
    out: dict[str, str] = {}
    for status, name in _RESULT_RE.findall(stdout):
        out.setdefault(name, status)
    return out
