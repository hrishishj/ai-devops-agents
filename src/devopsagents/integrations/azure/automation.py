"""Azure Automation job client (Entra token via managed identity)."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import httpx

API_VERSION = "2019-06-01"
TERMINAL = {"Completed", "Failed", "Stopped", "Suspended"}


class AutomationPort(Protocol):
    def start_job(self, runbook: str, params: dict[str, object]) -> str: ...

    def job_status(self, job_id: str) -> str: ...


class AutomationClient:  # pragma: no cover - needs live Azure; contract-tested via MockTransport
    def __init__(
        self,
        account_url: str,
        token_provider: Any = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = account_url.rstrip("/")
        self._http = httpx.Client(transport=transport, timeout=30.0)
        self._token = token_provider

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            from azure.identity import DefaultAzureCredential

            self._token = DefaultAzureCredential()
        tok = self._token.get_token("https://management.azure.com/.default").token
        return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def start_job(self, runbook: str, params: dict[str, object]) -> str:
        job_id = str(uuid.uuid4())  # PUT is idempotent on the job GUID
        body = {
            "properties": {"runbook": {"name": runbook}, "parameters": {k: str(v) for k, v in params.items()}}
        }
        r = self._http.put(
            f"{self._base}/jobs/{job_id}",
            params={"api-version": API_VERSION},
            headers=self._headers(),
            json=body,
        )
        r.raise_for_status()
        return job_id

    def job_status(self, job_id: str) -> str:
        r = self._http.get(
            f"{self._base}/jobs/{job_id}", params={"api-version": API_VERSION}, headers=self._headers()
        )
        r.raise_for_status()
        return str(r.json()["properties"]["status"])
