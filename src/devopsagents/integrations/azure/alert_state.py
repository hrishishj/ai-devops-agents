"""Recovery verification via Azure Monitor alert state (Alerts Management API).

A metric alert auto-resolves when its condition clears, so `monitorCondition == "Resolved"` after a
remediation is real evidence of recovery. Anything uncertain returns None (-> the agent escalates).
"""

from __future__ import annotations

from typing import Any

import httpx

from devopsagents.domain.events import AlertEvent

API_VERSION = "2019-05-05-preview"


class AlertStateVerifier:
    def __init__(
        self,
        token_provider: Any = None,
        transport: httpx.BaseTransport | None = None,
        base_url: str = "https://management.azure.com",
    ) -> None:
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=30.0)
        self._token = token_provider

    def healthy(self, alert: AlertEvent) -> bool | None:
        # essentials.alertId is the full ARM resource id of the alert instance
        if not alert.alert_id.startswith("/subscriptions/"):
            return None
        try:
            if self._token is None:  # pragma: no cover - needs azure-identity
                from azure.identity import DefaultAzureCredential

                self._token = DefaultAzureCredential()
            tok = self._token.get_token("https://management.azure.com/.default").token
            r = self._http.get(
                alert.alert_id,
                params={"api-version": API_VERSION},
                headers={"Authorization": f"Bearer {tok}"},
            )
            if r.status_code != 200:
                return None
            cond = str(r.json().get("properties", {}).get("essentials", {}).get("monitorCondition", ""))
        except Exception:
            return None
        if cond.lower() == "resolved":
            return True
        if cond.lower() == "fired":
            return False
        return None
