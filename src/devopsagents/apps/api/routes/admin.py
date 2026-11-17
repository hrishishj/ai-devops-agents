from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from devopsagents.agents.incident.workflow import AGENT_VERSION
from devopsagents.apps.api.auth import authenticate_admin
from devopsagents.apps.container import Container
from devopsagents.apps.dispatch import dispatch
from devopsagents.domain.jobs import AgentKind

router = APIRouter(prefix="/admin")


class Approval(BaseModel):
    # Only used in `token` auth mode (dev/pilot), where identity is self-declared.
    # In `easyauth` mode the approver is the validated Entra principal and this field is ignored.
    approver: str | None = Field(default=None, min_length=1, max_length=128)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, object]:
    c: Container = request.app.state.container
    authenticate_admin(request, c.settings)
    job = c.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"job": job.model_dump(mode="json", exclude={"payload"}), "result": c.store.result(job_id)}


@router.post("/incidents/{target}/approve", status_code=202)
def approve(target: str, request: Request, body: Approval | None = None) -> dict[str, object]:
    c: Container = request.app.state.container
    principal = authenticate_admin(request, c.settings, role=c.settings.approver_role)
    if principal is not None:
        approver = principal.name
    elif body is not None and body.approver:
        approver = body.approver
    else:
        raise HTTPException(status_code=400, detail="approver is required in token auth mode")

    found = c.store.find_latest(AgentKind.INCIDENT, target)
    if found is None:
        raise HTTPException(status_code=409, detail="no action awaiting approval for this incident")
    analysis_job, result = found
    if not result or result.get("status") != "awaiting_approval" or not result.get("pending_action"):
        raise HTTPException(status_code=409, detail="no action awaiting approval for this incident")

    expires_at = result.get("expires_at")
    if isinstance(expires_at, int | float) and c.store.now() > float(expires_at):
        raise HTTPException(status_code=410, detail="approval expired; the alert will re-trigger analysis")

    required = int(result.get("required_approvals", 1))
    if required > 1 and principal is None:
        raise HTTPException(
            status_code=403,
            detail="this action needs two verified approvers; use admin_auth=github or easyauth",
        )

    # Votes are recorded in the audit log, once per approver per pending proposal.
    revision = analysis_job.revision
    voters = _voters(c, target, revision)
    if approver.lower() not in {v.lower() for v in voters}:
        c.store.audit(approver, "approval.vote", target, {"revision": revision})
        voters.append(approver)
    if len(voters) < required:
        return {
            "status": "pending_more_approvals",
            "approvals": len(voters),
            "required": required,
            "approvers": voters,
        }

    job, created = dispatch(
        c,
        AgentKind.INCIDENT,
        target,
        f"approve-{revision}",
        AGENT_VERSION,
        {
            "mode": "execute_approved",
            "approver": ", ".join(voters),
            "approvers": voters,
            "action": result["pending_action"],
            "alert": result["alert"],
            "evidence": result.get("evidence", []),
            "expires_at": expires_at,
        },
    )
    c.store.audit(
        approver,
        "approval.requested",
        target,
        {"job": job.id, "verified_identity": principal is not None, "approvers": voters},
    )
    return {
        "status": "accepted" if created else "already_approved",
        "job_id": job.id,
        "approvals": len(voters),
        "required": required,
    }


def _voters(c: Container, target: str, revision: str) -> list[str]:
    seen: dict[str, str] = {}
    for e in c.store.audit_events(target):
        if e["event"] == "approval.vote" and e["data"].get("revision") == revision:
            seen.setdefault(str(e["actor"]).lower(), str(e["actor"]))
    return list(seen.values())
