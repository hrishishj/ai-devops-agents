from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from devopsagents.agents.review.workflow import AGENT_VERSION as REVIEW_V
from devopsagents.agents.security.workflow import AGENT_VERSION as SECURITY_V
from devopsagents.apps.container import Container
from devopsagents.apps.dispatch import dispatch
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.github.webhooks import parse_pull_request, verify_signature

router = APIRouter()


@router.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request) -> dict[str, object]:
    c: Container = request.app.state.container
    body = await request.body()
    if not verify_signature(
        c.settings.github_webhook_secret.get_secret_value(), body, request.headers.get("x-hub-signature-256")
    ):
        raise HTTPException(status_code=401, detail="invalid signature")
    event = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")
    if event == "ping":
        return {"status": "pong"}
    if event != "pull_request":
        return {"status": "ignored", "event": event}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    pr = parse_pull_request(delivery, payload)
    if pr is None:
        return {"status": "ignored", "reason": "action not handled"}

    data: dict[str, object] = {
        "repo": pr.repo,
        "number": pr.number,
        "head_sha": pr.head_sha,
        "installation_id": pr.installation_id,
        "delivery": delivery,
    }
    jobs = {}
    for agent, version in ((AgentKind.REVIEW, REVIEW_V), (AgentKind.SECURITY, SECURITY_V)):
        job, created = dispatch(c, agent, pr.target, pr.head_sha, version, data)
        jobs[agent.value] = {"id": job.id, "created": created}
    return {"status": "accepted", "jobs": jobs}
