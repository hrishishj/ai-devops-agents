"""End-to-end: Azure Monitor alert -> worker -> approval request -> HTTP approve -> execution."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import SecretStr

from devopsagents.agents.incident.workflow import IncidentAgent
from devopsagents.apps.api.main import create_app
from devopsagents.apps.container import LogNotifier, build_container
from devopsagents.apps.worker.main import Worker
from devopsagents.core.config.settings import Settings
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import LLMGateway
from devopsagents.core.policy.engine import PolicyConfig, PolicyEngine
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.jobs import AgentKind
from tests.unit.test_incident import FakeAuto, FakeEvidence, FakeVerifier

PARAMS = {"service_type": "ContainerApp", "service_name": "api", "resource_group": "rg1"}


def test_alert_to_approval_to_execution(repo_root):
    s = Settings(database_url="sqlite+pysqlite://", alert_token=SecretStr("a"), admin_token=SecretStr("b"))
    backend = ScriptedBackend(
        replies=[
            {
                "summary": "api down",
                "severity": "high",
                "confidence": 0.95,
                "recommended_runbook": "restart-service",
                "runbook_params": PARAMS,
                "hypotheses": [],
                "ticket_body": "",
            }
        ]
    )
    c = build_container(s, backend=backend, base_dir=repo_root)
    auto = FakeAuto()
    llm = LLMGateway(backend, c.store, sleep=lambda x: None)
    policy = PolicyEngine(PolicyConfig(), c.registry, c.store)
    c.handlers[AgentKind.INCIDENT] = IncidentAgent(
        llm,
        PromptRegistry(repo_root / "prompts"),
        c.store,
        c.registry,
        policy,
        FakeEvidence(),
        auto,
        FakeVerifier(True),
        LogNotifier(),
        sleep=lambda x: None,
    ).handle
    client = TestClient(create_app(c))

    payload = {
        "data": {
            "essentials": {
                "alertId": "x1",
                "alertRule": "api down",
                "monitorCondition": "Fired",
                "severity": "Sev1",
                "alertTargetIDs": ["/subscriptions/s/resourceGroups/prod-rg/api"],
            }
        }
    }
    r = client.post("/webhooks/azure-monitor", json=payload, headers={"x-alert-token": "a"})
    job = c.store.get(r.json()["job_id"])

    assert Worker(c, [AgentKind.INCIDENT]).tick() == {"succeeded": 1}
    assert auto.started == []  # prod: nothing runs without a human
    assert c.store.result(job.id)["status"] == "awaiting_approval"

    ok = client.post(
        f"/admin/incidents/{job.target}/approve", json={"approver": "alice"}, headers={"x-admin-token": "b"}
    )
    assert ok.status_code == 202
    again = client.post(
        f"/admin/incidents/{job.target}/approve", json={"approver": "alice"}, headers={"x-admin-token": "b"}
    )
    assert again.json()["status"] == "already_approved"  # idempotent

    assert Worker(c, [AgentKind.INCIDENT]).tick() == {"succeeded": 1}
    assert auto.started == [("Restart-Service", PARAMS)]
    trail = [(e["actor"], e["event"]) for e in c.store.audit_events()]
    assert ("alice", "approval.requested") in trail and ("alice", "action.executed") in trail
