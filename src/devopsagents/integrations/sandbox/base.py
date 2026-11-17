"""Sandbox port for running untrusted code (generated tests against PR code).

SECURITY: PR code is attacker-controlled. It must NEVER execute in the worker process, which holds a
managed identity. Adapters must run it in an isolated environment with no identity and no secrets.
"""

from __future__ import annotations

import json
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


RESULT_MARKER = "@@RESULT@@"

# Script executed INSIDE the isolated environment. It receives {files, target, timeout} as JSON, writes the
# files into a fresh temp directory, runs pytest there, and prints one marked JSON result line.
RUNNER_BODY = r"""
import json, subprocess, sys, tempfile, pathlib
payload = json.loads(__PAYLOAD__)
root = pathlib.Path(tempfile.mkdtemp(prefix="gen-"))
for rel, content in payload["files"].items():
    dest = (root / rel).resolve()
    if root.resolve() not in dest.parents:
        continue
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
try:
    cmd = [sys.executable, "-m", "pytest", "-q", "-rA", "--tb=short", "-p", "no:cacheprovider"]
    p = subprocess.run(
        cmd + [payload["target"]], cwd=root, capture_output=True, text=True, timeout=payload["timeout"],
    )
    out = {"code": p.returncode, "stdout": p.stdout[-20000:], "stderr": p.stderr[-4000:],
           "timeout": False}
except subprocess.TimeoutExpired:
    out = {"code": -1, "stdout": "", "stderr": "", "timeout": True}
print("@@RESULT@@" + json.dumps(out))
"""


def inline_runner(payload: dict[str, object]) -> str:
    """Runner with the payload embedded (for APIs that only accept a code string)."""
    return RUNNER_BODY.replace("__PAYLOAD__", repr(json.dumps(payload)))


STDIN_RUNNER = RUNNER_BODY.replace("__PAYLOAD__", "sys.stdin.read()")  # payload arrives on stdin


def parse_runner_output(stdout: str, stderr: str = "") -> SandboxResult:
    """Extract the marked result; anything unexpected means the sandbox did not run the job."""
    marker = stdout.rfind(RESULT_MARKER)
    if marker < 0:
        raise SandboxUnavailable(f"no result from sandbox: {stderr[-300:] or stdout[-300:]}")
    try:
        data = json.loads(stdout[marker + len(RESULT_MARKER) :])
    except json.JSONDecodeError as exc:
        raise SandboxUnavailable("sandbox returned malformed output") from exc
    if "No module named pytest" in data.get("stderr", "") + data.get("stdout", ""):
        raise SandboxUnavailable("pytest is not installed in the sandbox image")
    return SandboxResult(data["code"], data["stdout"], data["stderr"], bool(data["timeout"]))
