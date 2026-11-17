from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from devopsagents.agents.review.workflow import ReviewAgent
from devopsagents.apps.api.main import create_app
from devopsagents.apps.container import build_container
from devopsagents.apps.worker.main import Worker
from devopsagents.core.config.settings import Settings
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import LLMGateway
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.jobs import AgentKind
from tests.unit.test_review import FakeGH

SECRET = "whsec"
ALERT_TOKEN = "alert-tok"
ADMIN = "admin-tok"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def pr_payload(sha="sha1", action="opened", draft=False):
    return {
        "action": action,
        "number": 7,
        "installation": {"id": 99},
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "draft": draft, "head": {"sha": sha}, "base": {"sha": "b"}},
    }


@pytest.fixture
def env(repo_root):
    settings = Settings(
        database_url="sqlite+pysqlite://",
        github_webhook_secret=SecretStr(SECRET),
        alert_token=SecretStr(ALERT_TOKEN),
        admin_token=SecretStr(ADMIN),
    )
    backend = ScriptedBackend(
        replies=[
            {
                "candidates": [
                    {
                        "path": "app/db.py",
                        "line": 11,
                        "quote": 'f"SELECT',
                        "severity": "high",
                        "category": "security",
                        "title": "SQL injection",
                        "detail": "d",
                        "confidence": 0.9,
                    }
                ]
            }
        ]
    )
    c = build_container(settings, backend=backend, base_dir=repo_root)
    gh = FakeGH()
    llm = LLMGateway(backend, c.store, sleep=lambda s: None)
    c.handlers[AgentKind.REVIEW] = ReviewAgent(gh, llm, PromptRegistry(repo_root / "prompts"), c.store).handle
    return c, TestClient(create_app(c)), gh


def post_pr(client, payload, sig=None, event="pull_request"):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "x-hub-signature-256": sig if sig is not None else sign(body),
            "x-github-event": event,
            "x-github-delivery": "d1",
        },
    )


def test_rejects_bad_or_missing_signature(env):
    _, client, _ = env
    assert post_pr(client, pr_payload(), sig="sha256=bad").status_code == 401
    body = json.dumps(pr_payload()).encode()
    assert (
        client.post("/webhooks/github", content=body, headers={"x-github-event": "pull_request"}).status_code
        == 401
    )


def test_unconfigured_secret_fails_closed(repo_root):
    c = build_container(Settings(database_url="sqlite+pysqlite://"), base_dir=repo_root)
    client = TestClient(create_app(c))
    assert post_pr(client, pr_payload(), sig=sign(json.dumps(pr_payload()).encode())).status_code == 401


def test_webhook_to_worker_to_posted_review(env):
    c, client, gh = env
    r = post_pr(client, pr_payload())
    assert r.status_code == 202 and set(r.json()["jobs"]) == {"review", "security"}
    counts = Worker(c, [AgentKind.REVIEW]).tick()  # security handler absent (no GitHub App)
    assert counts == {"succeeded": 1}
    assert len(gh.posted) == 1 and gh.posted[0][2][0].line == 11
    # redelivery of the same webhook is a no-op
    again = post_pr(client, pr_payload()).json()["jobs"]
    assert all(not j["created"] for j in again.values())
    assert Worker(c, [AgentKind.REVIEW]).tick() == {}


def test_new_push_supersedes_pending_job(env):
    c, client, gh = env
    post_pr(client, pr_payload("sha1"))
    post_pr(client, pr_payload("sha2"))
    ids = c.store.reclaimable(AgentKind.REVIEW)
    assert len(ids) == 1 and c.store.get(ids[0]).revision == "sha2"


def test_ignored_events_and_drafts(env):
    _, client, _ = env
    assert post_pr(client, pr_payload(action="closed")).json()["status"] == "ignored"
    assert post_pr(client, pr_payload(draft=True)).json()["status"] == "ignored"
    assert post_pr(client, {}, event="push").json()["status"] == "ignored"
    assert post_pr(client, {}, event="ping").json()["status"] == "pong"


def az_payload(rule="High CPU", res="/subscriptions/s/resourceGroups/rg-staging/providers/x/api"):
    return {
        "data": {
            "essentials": {
                "alertId": "a-1",
                "alertRule": rule,
                "severity": "Sev2",
                "monitorCondition": "Fired",
                "alertTargetIDs": [res],
            }
        }
    }


def test_alert_auth_and_correlation(env):
    c, client, _ = env
    assert client.post("/webhooks/azure-monitor", json=az_payload()).status_code == 401
    h = {"x-alert-token": ALERT_TOKEN}
    first = client.post("/webhooks/azure-monitor", json=az_payload(), headers=h).json()
    second = client.post("/webhooks/azure-monitor", json=az_payload(), headers=h).json()
    assert first["status"] == "accepted" and not first["correlated"]
    assert second["correlated"] and second["job_id"] == first["job_id"]
    job = c.store.get(first["job_id"])
    assert job.payload["alert"]["environment"] == "staging"


def test_resolved_alerts_ignored_and_unknown_env_is_strict():
    from devopsagents.integrations.azure.alerts import infer_environment, parse_azure_monitor

    p = az_payload()
    p["data"]["essentials"]["monitorCondition"] = "Resolved"
    assert parse_azure_monitor(p) is None
    assert infer_environment("/subscriptions/s/resourceGroups/mystery/x") == "prod"
    assert infer_environment("/x", {"environment": "Staging"}) == "staging"


def test_incident_approval_flow_over_http(env):
    c, client, _ = env
    h = {"x-alert-token": ALERT_TOKEN}
    job_id = client.post("/webhooks/azure-monitor", json=az_payload(res="/s/prod/api"), headers=h).json()[
        "job_id"
    ]
    job = c.store.get(job_id)
    assert client.post(f"/admin/incidents/{job.target}/approve", json={"approver": "a"}).status_code == 401
    adm = {"x-admin-token": ADMIN}
    # nothing is awaiting approval yet
    assert (
        client.post(f"/admin/incidents/{job.target}/approve", json={"approver": "a"}, headers=adm).status_code
        == 409
    )


def test_health_endpoints(env):
    _, client, _ = env
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").json()["status"] == "ok"
    assert client.get("/docs").status_code == 404


def test_container_uses_real_verifier_only_when_automation_configured(repo_root):
    from devopsagents.apps.container import NullVerifier, build_container

    def verifier_of(**kw):
        c = build_container(Settings(database_url="sqlite+pysqlite://", **kw), base_dir=repo_root)
        return c.handlers[AgentKind.INCIDENT].__self__._verifier

    assert isinstance(verifier_of(), NullVerifier)
    assert type(verifier_of(automation_account_url="https://x/aa")).__name__ == "AlertStateVerifier"


def _principal(name, roles):
    import base64

    claims = [{"typ": "roles", "val": r} for r in roles]
    blob = base64.b64encode(json.dumps({"claims": claims}).encode()).decode()
    return {"x-ms-client-principal": blob, "x-ms-client-principal-name": name}


def _pending_incident(repo_root, **settings_kw):
    from devopsagents.domain.events import AlertEvent

    s = Settings(database_url="sqlite+pysqlite://", **settings_kw)
    c = build_container(s, backend=ScriptedBackend(replies=[]), base_dir=repo_root)
    alert = AlertEvent(alert_id="a", source="x", title="t", resource_id="/r", environment="prod")
    action = {
        "runbook": "restart-service",
        "environment": "prod",
        "target": "/r",
        "incident_id": "inc",
        "params": {"service_type": "ContainerApp", "service_name": "api", "resource_group": "rg"},
    }
    job, _ = c.store.enqueue(AgentKind.INCIDENT, "inc", "r", "v1", {})
    c.store.lease(job.id)
    c.store.complete(
        job.id,
        {
            "status": "awaiting_approval",
            "pending_action": action,
            "alert": alert.model_dump(mode="json"),
            "evidence": ["e"],
        },
    )
    return c, TestClient(create_app(c))


def test_easyauth_uses_verified_principal_and_ignores_body(repo_root):
    c, client = _pending_incident(repo_root, admin_auth="easyauth")
    url = "/admin/incidents/inc/approve"
    assert client.post(url, json={"approver": "mallory"}).status_code == 401  # no principal
    assert client.post(url, headers=_principal("bob@x.com", ["Reader"])).status_code == 403  # wrong role
    ok = client.post(
        url, json={"approver": "mallory"}, headers=_principal("alice@x.com", ["Incident.Approver"])
    )
    assert ok.status_code == 202
    ev = [e for e in c.store.audit_events("inc") if e["event"] == "approval.requested"][0]
    assert ev["actor"] == "alice@x.com" and ev["data"]["verified_identity"] is True


def test_easyauth_rejects_malformed_principal_header(repo_root):
    _, client = _pending_incident(repo_root, admin_auth="easyauth")
    r = client.post("/admin/incidents/inc/approve", headers={"x-ms-client-principal": "!!notb64!!"})
    assert r.status_code == 401


def test_token_mode_requires_declared_approver(repo_root):
    _, client = _pending_incident(repo_root, admin_token=SecretStr("t"))
    h = {"x-admin-token": "t"}
    assert client.post("/admin/incidents/inc/approve", headers=h).status_code == 400
    assert client.post("/admin/incidents/inc/approve", json={"approver": "a"}, headers=h).status_code == 202
