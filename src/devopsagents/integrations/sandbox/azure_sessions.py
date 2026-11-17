"""Sandbox adapter for Azure Container Apps dynamic sessions (Python code interpreter).

Sessions are Hyper-V isolated, hold no managed identity and have egress disabled by default, which is
exactly the isolation untrusted PR code needs. The session image must provide `pytest`; if it does not,
the adapter raises SandboxUnavailable and test generation is skipped (never run locally instead).

NOTE: written against the documented code-execution API; it has only been contract-tested with a mock
transport. Validate against a real session pool before enabling.
"""

from __future__ import annotations

from typing import Any

import httpx

from devopsagents.integrations.sandbox.base import (
    SandboxResult,
    SandboxUnavailable,
    inline_runner,
    parse_runner_output,
)

API_VERSION = "2024-10-02-preview"


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
        code = inline_runner({"files": files, "target": target, "timeout": timeout_seconds})
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
        return parse_runner_output(str(props.get("stdout", "")), str(props.get("stderr", "")))
