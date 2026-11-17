from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from devopsagents.apps.container import Container

router = APIRouter()


@router.get("/health")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def readiness(request: Request) -> JSONResponse:
    c: Container = request.app.state.container
    try:
        dead = c.store.dead_letter_count()  # also proves the DB is reachable
    except Exception:
        return JSONResponse({"status": "unavailable", "db": "down"}, status_code=503)
    return JSONResponse({"status": "ok", "dead_jobs": dead})
