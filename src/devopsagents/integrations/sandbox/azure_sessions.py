"""Sandbox adapter for Azure Container Apps dynamic sessions (Python code interpreter).

Sessions are Hyper-V isolated, hold no managed identity and have egress disabled by default, which is
exactly the isolation untrusted PR code needs. The session image must provide `pytest`; if it does not,
the adapter raises SandboxUnavailable and test generation is skipped (never run locally instead).

NOTE: written against the documented code-execution API; it has only been contract-tested with a mock
transport. Validate against a real session pool before enabling.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from devopsagents.integrations.sandbox.base import SandboxResult, SandboxUnavailable

API_VERSION = "2024-10-02-preview"

# Executed inside the session. Receives files + target via JSON, runs pytest in a temp dir.
_RUNNER = r"""
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


class AzureSessionsSandbox:  # pragma: no cover - live Azure; contract-tested with a mock transport
    def __init__(
        self,
        pool_endpoint: str,
        session_id: str,
        token_provider: Any = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = pool_endpoint.rstrip("/")
        self._session = session_id
        self._token = token_provider
        self._http = httpx.Client(transport=transport, timeout=180.0)

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            from azure.identity import DefaultAzureCredential

            self._token = DefaultAzureCredential()
        tok = self._token.get_token("https://dynamicsessions.io/.default").token
        return {"Authorization": f"Bearer {tok}"}

    def run_pytest(self, files: dict[str, str], target: str, timeout_seconds: int = 60) -> SandboxResult:
        payload = json.dumps({"files": files, "target": target, "timeout": timeout_seconds})
        code = _RUNNER.replace("__PAYLOAD__", repr(payload))
        try:
            r = self._http.post(
                f"{self._endpoint}/code/execute",
                params={"api-version": API_VERSION, "identifier": self._session},
                headers=self._headers(),
                json={
                    "properties": {"codeInputType": "inline", "executionType": "synchronous", "code": code}
                },
            )
            r.raise_for_status()
            props = r.json().get("properties", {})
        except (httpx.HTTPError, ValueError) as exc:
            raise SandboxUnavailable(f"session call failed: {exc}") from exc
        stdout = str(props.get("stdout", ""))
        marker = stdout.rfind("@@RESULT@@")
        if marker < 0:
            raise SandboxUnavailable(f"no result from session: {str(props.get('stderr', ''))[:300]}")
        data = json.loads(stdout[marker + len("@@RESULT@@") :])
        if "No module named pytest" in data.get("stderr", "") + data.get("stdout", ""):
            raise SandboxUnavailable("pytest is not installed in the session image")
        return SandboxResult(data["code"], data["stdout"], data["stderr"], bool(data["timeout"]))
