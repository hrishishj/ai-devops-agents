"""Normalise Azure Monitor common-alert-schema payloads into AlertEvent."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from devopsagents.domain.events import AlertEvent

_ENV_RE = re.compile(r"(?:^|[-_/])(prod|production|staging|stage|dev|test)(?:$|[-_/])", re.I)
_ENV_MAP = {"production": "prod", "stage": "staging", "test": "dev"}


def infer_environment(resource_id: str, tags: dict[str, str] | None = None) -> str:
    """Environment comes from an explicit tag first, then the resource path. Unknown -> prod
    (the strictest policy), never dev."""
    if tags and tags.get("environment"):
        env = tags["environment"].lower()
        return _ENV_MAP.get(env, env)
    m = _ENV_RE.search(resource_id)
    if m:
        env = m.group(1).lower()
        return _ENV_MAP.get(env, env)
    return "prod"


def parse_azure_monitor(payload: dict[str, Any]) -> AlertEvent | None:
    data = payload.get("data") or {}
    ess = data.get("essentials") or {}
    if not ess.get("alertId"):
        return None
    if str(ess.get("monitorCondition", "")).lower() == "resolved":
        return None  # resolutions are not incidents
    targets = ess.get("alertTargetIDs") or [""]
    resource = str(targets[0])
    sev = str(ess.get("severity", "Sev3")).lower()
    return AlertEvent(
        alert_id=str(ess["alertId"]),
        source="azure_monitor",
        title=str(ess.get("alertRule", "unnamed alert")),
        severity=sev,
        resource_id=resource,
        environment=infer_environment(resource),
        fired_at=str(ess.get("firedDateTime", "")),
        raw={"description": str(ess.get("description", ""))[:2000]},
    )


def incident_revision(alert: AlertEvent, bucket: str) -> str:
    """Repeats of the same alert within a time bucket correlate into one incident job."""
    return hashlib.sha256(f"{alert.fingerprint}|{bucket}".encode()).hexdigest()[:16]


def incident_target(alert: AlertEvent) -> str:
    return "incident-" + hashlib.sha256(alert.fingerprint.encode()).hexdigest()[:12]
