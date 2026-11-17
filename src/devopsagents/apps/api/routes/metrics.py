from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from devopsagents.apps.api.auth import require_bearer
from devopsagents.apps.container import Container
from devopsagents.core.telemetry.metrics import CONTENT_TYPE, render_prometheus

router = APIRouter()


@router.get("/metrics")
def metrics(request: Request) -> Response:
    """Prometheus scrape endpoint. Disabled (404) until DA_METRICS_TOKEN is set, then bearer-protected."""
    c: Container = request.app.state.container
    token = c.settings.metrics_token.get_secret_value()
    if not token:
        raise HTTPException(status_code=404, detail="not found")
    require_bearer(request, token)
    body = render_prometheus(c.store.metrics_snapshot(), kill_switch=c.settings.kill_switch, version="0.1.0")
    return Response(content=body, media_type=CONTENT_TYPE)
