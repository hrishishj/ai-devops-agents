"""Read-only evidence & verification ports for incidents, plus a Log Analytics adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from devopsagents.domain.events import AlertEvent


@dataclass(frozen=True)
class Evidence:
    source: str
    query: str
    summary: str

    def render(self) -> str:
        return f"{self.source}: {self.summary} (query: {self.query})"


class EvidenceSource(Protocol):
    def gather(self, alert: AlertEvent) -> list[Evidence]: ...


class Verifier(Protocol):
    def healthy(self, alert: AlertEvent) -> bool | None:
        """True = recovered, False = still failing, None = cannot determine."""
        ...


# Fixed KQL templates only — the model never writes queries.
_TEMPLATES = {
    "recent_errors": (
        "AppTraces | where TimeGenerated > ago(15m) and SeverityLevel >= 3 "
        "| summarize errors=count() by bin(TimeGenerated, 5m)"
    ),
    "recent_deployments": (
        "AzureActivity | where TimeGenerated > ago(2h) and OperationNameValue has 'write' "
        "| summarize changes=count() by OperationNameValue | top 5 by changes"
    ),
}


class LogAnalyticsEvidence:  # pragma: no cover - needs live Azure
    def __init__(
        self,
        workspace_id: str,
        token_provider: Any = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._ws = workspace_id
        self._http = httpx.Client(transport=transport, timeout=30.0)
        self._token = token_provider

    def gather(self, alert: AlertEvent) -> list[Evidence]:
        if self._token is None:
            from azure.identity import DefaultAzureCredential

            self._token = DefaultAzureCredential()
        tok = self._token.get_token("https://api.loganalytics.io/.default").token
        out: list[Evidence] = []
        for name, kql in _TEMPLATES.items():
            r = self._http.post(
                f"https://api.loganalytics.io/v1/workspaces/{self._ws}/query",
                headers={"Authorization": f"Bearer {tok}"},
                json={"query": kql},
            )
            if r.status_code == 200:
                rows = r.json().get("tables", [{}])[0].get("rows", [])
                out.append(Evidence("log-analytics", name, f"{len(rows)} row(s): {rows[:5]}"))
        return out
