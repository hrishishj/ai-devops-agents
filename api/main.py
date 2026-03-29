"""
FastAPI Application — Webhook Receiver & Agent REST API

Routes
------
  POST /webhooks/github       — GitHub webhook events
  POST /webhooks/azure-monitor — Azure Monitor alert webhooks
  GET  /health                — liveness probe
  GET  /health/ready          — readiness probe (checks Redis, Cosmos)
  POST /api/v1/review         — direct code review trigger (for CI)
  POST /api/v1/it-ops/analyse — direct IT Ops analysis (for CI)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from azure.servicebus.aio import ServiceBusClient
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.coding_copilot.agent import CodingCopilotAgent
from agents.it_ops.agent import ItOpsAgent
from agents.test_security.agent import TestSecurityAgent
from shared.config import get_settings
from shared.redis_cache import get_cache
from shared.telemetry import get_tracer, init_telemetry

logger = logging.getLogger(__name__)
_settings = get_settings()

# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_telemetry()
    logger.info("AI DevOps Agents API starting up.")
    yield
    logger.info("AI DevOps Agents API shutting down.")


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(
    title="AI DevOps Agents",
    description="Webhook receiver and agent API for AI-assisted development and IT ops.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

tracer = get_tracer(__name__)

# --------------------------------------------------------------------------- #
# Agent singletons
# --------------------------------------------------------------------------- #

def _get_coding_agent() -> CodingCopilotAgent:
    agent = CodingCopilotAgent()
    agent.__post_init__()
    return agent


def _get_itops_agent() -> ItOpsAgent:
    agent = ItOpsAgent()
    agent.__post_init__()
    return agent


def _get_security_agent() -> TestSecurityAgent:
    agent = TestSecurityAgent()
    agent.__post_init__()
    return agent


# --------------------------------------------------------------------------- #
# HMAC signature verification
# --------------------------------------------------------------------------- #

def _verify_github_signature(body: bytes, signature_header: str | None) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    secret = _settings.github_webhook_secret
    if not secret:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature check.")
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# --------------------------------------------------------------------------- #
# Request / Response models
# --------------------------------------------------------------------------- #

class DirectReviewRequest(BaseModel):
    repo_full_name: str
    pr_number: int


class DirectItOpsRequest(BaseModel):
    alert_id: str
    source: str
    log_payload: str
    metadata: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    status: str
    components: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# GitHub Webhook
# --------------------------------------------------------------------------- #

@app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    body = await request.body()

    if not _verify_github_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    event = x_github_event or "unknown"
    logger.info("GitHub event received: %s", event)

    with tracer.start_as_current_span("webhook.github") as span:
        span.set_attribute("github.event", event)

        if event == "pull_request":
            action = payload.get("action", "")
            if action in ("opened", "synchronize", "reopened"):
                repo = payload["repository"]["full_name"]
                pr_number = payload["pull_request"]["number"]
                span.set_attribute("github.repo", repo)
                span.set_attribute("github.pr_number", pr_number)

                # Dispatch to all three agents asynchronously
                background_tasks.add_task(_run_coding_review, repo, pr_number)
                background_tasks.add_task(_run_security_scan, repo, pr_number)

        elif event == "push":
            # Future: trigger pipeline-level analysis
            pass

    return {"status": "accepted", "event": event}


# --------------------------------------------------------------------------- #
# Azure Monitor Alert Webhook
# --------------------------------------------------------------------------- #

@app.post("/webhooks/azure-monitor", status_code=status.HTTP_202_ACCEPTED)
async def azure_monitor_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    alert_id = payload.get("data", {}).get("essentials", {}).get("alertId", "unknown")
    description = payload.get("data", {}).get("essentials", {}).get("description", "")
    log_payload = json.dumps(payload, indent=2)
    metadata = {
        "subscription": payload.get("data", {}).get("essentials", {}).get("subscriptionId"),
        "resource_group": payload.get("data", {}).get("essentials", {}).get("resourceGroupName"),
        "alert_rule": payload.get("data", {}).get("essentials", {}).get("alertRule"),
    }

    background_tasks.add_task(
        _run_itops_analysis, alert_id, "azure_monitor", log_payload, metadata
    )
    return {"status": "accepted", "alert_id": alert_id}


# --------------------------------------------------------------------------- #
# Direct API triggers (for GitHub Actions CI steps)
# --------------------------------------------------------------------------- #

@app.post("/api/v1/review", status_code=status.HTTP_202_ACCEPTED)
async def trigger_review(req: DirectReviewRequest, background_tasks: BackgroundTasks):
    """Directly trigger a code review without a webhook event."""
    background_tasks.add_task(_run_coding_review, req.repo_full_name, req.pr_number)
    return {"status": "accepted", "repo": req.repo_full_name, "pr_number": req.pr_number}


@app.post("/api/v1/it-ops/analyse", status_code=status.HTTP_202_ACCEPTED)
async def trigger_itops(req: DirectItOpsRequest, background_tasks: BackgroundTasks):
    """Directly trigger IT Ops analysis."""
    background_tasks.add_task(
        _run_itops_analysis,
        req.alert_id,
        req.source,
        req.log_payload,
        req.metadata,
    )
    return {"status": "accepted", "alert_id": req.alert_id}


# --------------------------------------------------------------------------- #
# Health probes
# --------------------------------------------------------------------------- #

@app.get("/health", response_model=HealthResponse)
async def liveness():
    return HealthResponse(status="ok")


@app.get("/health/ready", response_model=HealthResponse)
async def readiness():
    components: dict[str, str] = {}
    overall = "ok"

    # Redis check
    try:
        cache = get_cache()
        components["redis"] = "ok" if cache.ping() else "degraded"
    except Exception:
        components["redis"] = "unavailable"
        overall = "degraded"

    if overall != "ok":
        return Response(
            content=HealthResponse(status=overall, components=components).model_dump_json(),
            status_code=503,
            media_type="application/json",
        )
    return HealthResponse(status=overall, components=components)


# --------------------------------------------------------------------------- #
# Background task helpers
# --------------------------------------------------------------------------- #

def _run_coding_review(repo_full_name: str, pr_number: int) -> None:
    try:
        agent = _get_coding_agent()
        agent.handle_pr(repo_full_name, pr_number)
    except Exception as exc:
        logger.error("Coding Co-Pilot failed for %s#%d: %s", repo_full_name, pr_number, exc, exc_info=True)


def _run_security_scan(repo_full_name: str, pr_number: int) -> None:
    try:
        agent = _get_security_agent()
        agent.handle_pr(repo_full_name, pr_number)
    except Exception as exc:
        logger.error("Test/Security agent failed for %s#%d: %s", repo_full_name, pr_number, exc, exc_info=True)


def _run_itops_analysis(
    alert_id: str,
    source: str,
    log_payload: str,
    metadata: dict[str, Any] | None,
) -> None:
    try:
        agent = _get_itops_agent()
        agent.handle_alert(alert_id, source, log_payload, metadata)
    except Exception as exc:
        logger.error("IT Ops agent failed for alert %s: %s", alert_id, exc, exc_info=True)
