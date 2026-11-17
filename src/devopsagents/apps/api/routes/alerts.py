from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request

from devopsagents.agents.incident.workflow import AGENT_VERSION
from devopsagents.apps.api.auth import require_alert_token, require_token
from devopsagents.apps.container import Container
from devopsagents.apps.dispatch import dispatch
from devopsagents.domain.events import AlertEvent
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.azure.alerts import (
    incident_revision,
    incident_target,
    parse_azure_monitor,
)
from devopsagents.integrations.prometheus.alerts import parse_alertmanager

router = APIRouter()


def _dispatch_alert(c: Container, alert: AlertEvent) -> tuple[str, bool]:
    bucket = datetime.fromtimestamp(c.store.now(), UTC).strftime("%Y%m%d%H")  # hourly correlation
    job, created = dispatch(
        c,
        AgentKind.INCIDENT,
        incident_target(alert),
        incident_revision(alert, bucket),
        AGENT_VERSION,
        {"mode": "analyse", "alert": alert.model_dump(mode="json")},
    )
    return job.id, not created


@router.post("/webhooks/azure-monitor", status_code=202)
async def azure_monitor(request: Request) -> dict[str, object]:
    c: Container = request.app.state.container
    require_token(request, c.settings.alert_token.get_secret_value(), "x-alert-token")
    try:
        payload = json.loads(await request.body())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    alert = parse_azure_monitor(payload)
    if alert is None:
        return {"status": "ignored"}
    job_id, correlated = _dispatch_alert(c, alert)
    return {"status": "accepted", "job_id": job_id, "correlated": correlated}


@router.post("/webhooks/alertmanager", status_code=202)
async def alertmanager(request: Request) -> dict[str, object]:
    """Prometheus Alertmanager webhook receiver (free, self-hosted)."""
    c: Container = request.app.state.container
    require_alert_token(request, c.settings.alert_token.get_secret_value())
    try:
        payload = json.loads(await request.body())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    alerts = parse_alertmanager(payload)
    if not alerts:
        return {"status": "ignored"}
    jobs = []
    for alert in alerts:
        job_id, correlated = _dispatch_alert(c, alert)
        jobs.append({"job_id": job_id, "correlated": correlated})
    return {"status": "accepted", "jobs": jobs}
