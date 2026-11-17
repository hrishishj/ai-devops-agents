"""Read-only Prometheus adapters: incident evidence and recovery verification.

Only fixed PromQL templates are used, and label values are validated before interpolation, so
neither the model nor an attacker-controlled alert label can inject query syntax.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from devopsagents.domain.events import AlertEvent
from devopsagents.integrations.azure.evidence import Evidence

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/@\-]{1,200}$")


def _safe(value: str) -> str | None:
    return value if _SAFE_LABEL.fullmatch(value) else None


class PrometheusClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def query(self, promql: str) -> list[dict[str, Any]]:
        r = self._http.get("/api/v1/query", params={"query": promql}, headers=self._headers)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise ValueError(f"prometheus query failed: {body.get('error', 'unknown')}")
        result: list[dict[str, Any]] = body["data"]["result"]
        return result


def _fmt(series: list[dict[str, Any]]) -> str:
    if not series:
        return "no series"
    vals = []
    for s in series[:5]:
        _, v = s.get("value", [0, "?"])
        labels = s.get("metric", {})
        tag = labels.get("instance") or labels.get("pod") or labels.get("job") or ""
        vals.append(f"{tag}={v}" if tag else str(v))
    more = f" (+{len(series) - 5} more)" if len(series) > 5 else ""
    return ", ".join(vals) + more


class PrometheusEvidence:
    def __init__(self, client: PrometheusClient) -> None:
        self._c = client

    def gather(self, alert: AlertEvent) -> list[Evidence]:
        name = _safe(alert.title)
        inst = _safe(alert.resource_id)
        queries: list[tuple[str, str]] = []
        if name:
            queries.append(("alert firing", f'ALERTS{{alertname="{name}",alertstate="firing"}}'))
        if inst:
            queries += [
                ("target up", f'up{{instance="{inst}"}}'),
                ("cpu cores used (5m)", f'rate(process_cpu_seconds_total{{instance="{inst}"}}[5m])'),
                ("resident memory bytes", f'process_resident_memory_bytes{{instance="{inst}"}}'),
            ]
        out: list[Evidence] = []
        for label, promql in queries:
            try:
                out.append(Evidence("prometheus", promql, f"{label}: {_fmt(self._c.query(promql))}"))
            except (httpx.HTTPError, ValueError, KeyError):
                continue  # missing evidence is fine; fabricated evidence is not
        return out


class PrometheusVerifier:
    """Recovered = the alert is no longer in the firing ALERTS series."""

    def __init__(self, client: PrometheusClient) -> None:
        self._c = client

    def healthy(self, alert: AlertEvent) -> bool | None:
        name = _safe(alert.title)
        if alert.source != "alertmanager" or not name:
            return None
        selector = f'alertname="{name}",alertstate="firing"'
        inst = _safe(alert.resource_id)
        if inst:
            selector += f',instance="{inst}"'
        try:
            return len(self._c.query(f"ALERTS{{{selector}}}")) == 0
        except (httpx.HTTPError, ValueError, KeyError):
            return None
