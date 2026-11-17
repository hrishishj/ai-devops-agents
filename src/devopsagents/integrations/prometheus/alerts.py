"""Normalise Prometheus Alertmanager webhook payloads (version 4) into AlertEvent."""

from __future__ import annotations

import hashlib
from typing import Any

from devopsagents.domain.events import AlertEvent

MAX_ALERTS_PER_REQUEST = 50
_RESOURCE_LABELS = ("instance", "pod", "service", "container", "job")
_ENV_LABELS = ("environment", "env")


def _clip(value: Any, limit: int = 300) -> str:
    return str(value)[:limit]


def parse_alertmanager(payload: dict[str, Any]) -> list[AlertEvent]:
    """One AlertEvent per *firing* alert. Resolved alerts are not incidents.

    Environment comes from an explicit `environment`/`env` label; anything else defaults to `prod`
    (the strictest policy), never to dev.
    """
    events: list[AlertEvent] = []
    group_key = str(payload.get("groupKey", ""))
    for a in (payload.get("alerts") or [])[:MAX_ALERTS_PER_REQUEST]:
        if a.get("status") != "firing":
            continue
        labels: dict[str, Any] = a.get("labels") or {}
        name = labels.get("alertname")
        if not name:
            continue
        resource = next((str(labels[k]) for k in _RESOURCE_LABELS if labels.get(k)), "")
        env = next((str(labels[k]).lower() for k in _ENV_LABELS if labels.get(k)), "prod")
        alert_id = (
            str(a.get("fingerprint") or "")
            or hashlib.sha256(f"{group_key}|{name}|{resource}".encode()).hexdigest()[:16]
        )
        annotations: dict[str, Any] = a.get("annotations") or {}
        events.append(
            AlertEvent(
                alert_id=alert_id,
                source="alertmanager",
                title=_clip(name, 200),
                severity=_clip(labels.get("severity", "unknown"), 40).lower(),
                resource_id=_clip(resource, 200),
                environment=env,
                fired_at=_clip(a.get("startsAt", ""), 40),
                raw={
                    "labels": {_clip(k, 60): _clip(v, 200) for k, v in list(labels.items())[:30]},
                    "summary": _clip(annotations.get("summary", ""), 500),
                    "description": _clip(annotations.get("description", ""), 1000),
                },
            )
        )
    return events
