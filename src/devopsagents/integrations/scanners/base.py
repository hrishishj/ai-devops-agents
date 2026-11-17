"""Scanner port, built-in secrets scanner, and a Semgrep adapter (sandboxed subprocess)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from devopsagents.core.llm.redact import SECRET_PATTERNS
from devopsagents.domain.findings import Severity


@dataclass(frozen=True)
class RawFinding:
    scanner: str
    rule: str
    severity: Severity
    path: str
    line: int
    message: str
    code: str = ""
    cwe: str = ""
    deterministic: bool = False  # high-precision rule: the LLM may explain but never dismiss


class Scanner(Protocol):
    name: str

    def scan(self, files: dict[str, str]) -> list[RawFinding]: ...


_HIGH_PRECISION = {"aws-access-key", "github-token", "private-key", "slack-token", "azure-storage-key"}


class SecretsScanner:
    name = "secrets"

    def scan(self, files: dict[str, str]) -> list[RawFinding]:
        out: list[RawFinding] = []
        for path, content in files.items():
            for lineno, line in enumerate(content.splitlines(), start=1):
                for p in SECRET_PATTERNS:
                    if p.regex.search(line):
                        out.append(
                            RawFinding(
                                scanner=self.name,
                                rule=p.rule,
                                severity=Severity.CRITICAL if p.rule in _HIGH_PRECISION else Severity.HIGH,
                                path=path,
                                line=lineno,
                                message=p.description,
                                code=_mask(line.strip()),
                                cwe="CWE-798",
                                deterministic=p.rule in _HIGH_PRECISION,
                            )
                        )
        return out


def _mask(line: str) -> str:
    """Never echo a secret back into comments/logs."""
    return line[:6] + "…[masked]" if len(line) > 12 else "[masked]"


_SEMGREP_SEV = {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM, "INFO": Severity.LOW}


class SemgrepScanner:
    """Runs `semgrep` on a throw-away directory. Skipped silently when not installed.

    Isolation: files are written to a fresh temp dir, semgrep is invoked with metrics off, a
    wall-clock timeout, and no shell. Deploy this worker with no egress to harden further.
    """

    name = "semgrep"

    def __init__(self, config: str = "p/default", timeout: int = 120) -> None:
        self._config = config
        self._timeout = timeout

    @property
    def available(self) -> bool:
        return shutil.which("semgrep") is not None

    def scan(self, files: dict[str, str]) -> list[RawFinding]:
        exe = shutil.which("semgrep")
        if not exe or not files:
            return []
        with tempfile.TemporaryDirectory(prefix="da-scan-") as tmp:
            root = Path(tmp)
            for rel, content in files.items():
                dest = (root / rel).resolve()
                if root.resolve() not in dest.parents:  # path traversal guard
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
            proc = subprocess.run(  # noqa: S603
                [exe, "scan", "--json", "--quiet", "--metrics=off", "--config", self._config, "."],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        return parse_semgrep(proc.stdout)


def parse_semgrep(stdout: str) -> list[RawFinding]:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    out: list[RawFinding] = []
    for r in data.get("results", []):
        extra = r.get("extra", {})
        meta = extra.get("metadata", {})
        cwe = meta.get("cwe", "")
        out.append(
            RawFinding(
                scanner="semgrep",
                rule=r.get("check_id", "semgrep"),
                severity=_SEMGREP_SEV.get(str(extra.get("severity", "")).upper(), Severity.MEDIUM),
                path=r.get("path", "").lstrip("./"),
                line=int(r.get("start", {}).get("line", 1)),
                message=extra.get("message", ""),
                code=(extra.get("lines") or "")[:200],
                cwe=cwe[0] if isinstance(cwe, list) and cwe else str(cwe),
            )
        )
    return out
