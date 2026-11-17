from __future__ import annotations

import json

import httpx
import pytest
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
from devopsagents.domain.events import AlertEvent
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.github.automation import GitHubActionsAutomation
from devopsagents.integrations.github.client import GitHubClient
from devopsagents.integrations.github.identity import GitHubIdentity, GitHubIdentityVerifier
from devopsagents.integrations.prometheus.alerts import MAX_ALERTS_PER_REQUEST, parse_alertmanager
from devopsagents.integrations.prometheus.client import (
    PrometheusClient,
    PrometheusEvidence,
    PrometheusVerifier,
)
from tests.unit.test_api_e2e import _pending_incident

PARAMS = {"service_type": "ContainerApp", "service_name": "api", "resource_group": "rg1"}


def am_alert(name="HighCPU", status="firing", **labels):
    base = {"alertname": name, "instance": "api-1:9100", "severity": "critical", **labels}
    return {
        "status": status,
        "labels": base,
        "fingerprint": f"fp-{name}",
        "annotations": {"summary": "cpu high", "description": "d"},
        "startsAt": "2026-11-17T10:00:00Z",
    }


def am_payload(*alerts):
    return {"version": "4", "groupKey": "g1", "alerts": list(alerts)}


# ---- Alertmanager parsing ----------------------------------------------------------------------
def test_only_firing_alerts_become_events_with_safe_defaults():
    events = parse_alertmanager(
        am_payload(am_alert("A"), am_alert("B", status="resolved"), {"status": "firing", "labels": {}})
    )
    assert [e.title for e in events] == ["A"]
    e = events[0]
    assert (e.alert_id, e.source, e.severity, e.resource_id) == (
        "fp-A",
        "alertmanager",
        "critical",
        "api-1:9100",
    )
    assert e.environment == "prod"  # no env label -> strictest policy, never dev
    assert e.raw["summary"] == "cpu high"


def test_environment_label_resource_precedence_and_caps():
    e = parse_alertmanager(am_payload(am_alert(env="Staging", pod="p1")))[0]
    assert e.environment == "staging" and e.resource_id == "api-1:9100"  # instance outranks pod
    many = [am_alert(f"A{i}") for i in range(MAX_ALERTS_PER_REQUEST + 20)]
    assert len(parse_alertmanager(am_payload(*many))) == MAX_ALERTS_PER_REQUEST
    no_fp = am_alert("Z")
    del no_fp["fingerprint"]
    assert len(parse_alertmanager(am_payload(no_fp))[0].alert_id) == 16  # stable hash fallback


# ---- Prometheus adapters -----------------------------------------------------------------------
def prom(handler):
    return PrometheusClient("http://prom:9090", transport=httpx.MockTransport(handler))


def ok(result):
    return httpx.Response(200, json={"status": "success", "data": {"result": result}})


def alert_event(**kw):
    base = dict(alert_id="a", source="alertmanager", title="HighCPU", resource_id="api-1:9100")
    base.update(kw)
    return AlertEvent(**base)


def test_evidence_runs_fixed_templates_and_formats_values():
    queries = []

    def handler(req):
        queries.append(req.url.params["query"])
        return ok([{"metric": {"instance": "api-1:9100"}, "value": [0, "0.97"]}])

    ev = PrometheusEvidence(prom(handler)).gather(alert_event())
    assert len(ev) == 4 and 'up{instance="api-1:9100"}' in queries
    assert 'ALERTS{alertname="HighCPU",alertstate="firing"}' in queries
    assert "api-1:9100=0.97" in ev[0].summary and ev[0].source == "prometheus"


def test_label_values_cannot_inject_promql():
    queries = []
    evil = 'x"} or vector(1) #'
    PrometheusEvidence(prom(lambda r: (queries.append(r.url.params["query"]), ok([]))[1])).gather(
        alert_event(title=evil, resource_id=evil)
    )
    assert queries == []  # unsafe labels are skipped, never interpolated


def test_evidence_skips_failed_queries_instead_of_inventing_data():
    assert PrometheusEvidence(prom(lambda r: httpx.Response(500))).gather(alert_event()) == []
    bad = httpx.Response(200, json={"status": "error", "error": "boom"})
    assert PrometheusEvidence(prom(lambda r: bad)).gather(alert_event()) == []


def test_verifier_recovered_still_firing_and_unknown():
    seen = []

    def handler(req):
        seen.append(req.url.params["query"])
        return ok([])

    assert PrometheusVerifier(prom(handler)).healthy(alert_event()) is True
    assert seen == ['ALERTS{alertname="HighCPU",alertstate="firing",instance="api-1:9100"}']
    still = PrometheusVerifier(prom(lambda r: ok([{"metric": {}, "value": [0, "1"]}])))
    assert still.healthy(alert_event()) is False
    assert PrometheusVerifier(prom(lambda r: httpx.Response(503))).healthy(alert_event()) is None
    assert PrometheusVerifier(prom(lambda r: ok([]))).healthy(alert_event(source="azure_monitor")) is None


# ---- GitHub Actions as the runbook executor ----------------------------------------------------
class FakeActions:
    """Pretends to be GitHub: records dispatches and serves a scripted run lifecycle."""

    def __init__(self, lifecycle=("in_progress", "completed"), conclusion="success"):
        self.dispatched, self.lifecycle, self.conclusion, self.polls = [], list(lifecycle), conclusion, 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/dispatches"):
            self.dispatched.append(json.loads(req.content))
            return httpx.Response(204)
        if req.url.path.endswith("/runs"):
            if not self.dispatched:
                return httpx.Response(200, json={"workflow_runs": []})
            cid = self.dispatched[-1]["inputs"]["correlation_id"]
            state = self.lifecycle[min(self.polls, len(self.lifecycle) - 1)]
            self.polls += 1
            run = {
                "name": f"runbook {cid}",
                "status": state,
                "conclusion": self.conclusion if state == "completed" else None,
            }
            return httpx.Response(
                200, json={"workflow_runs": [{"name": "other", "status": "completed"}, run]}
            )
        return httpx.Response(404)


def actions(fake):
    gh = GitHubClient(token="ghs_t", transport=httpx.MockTransport(fake))
    return GitHubActionsAutomation(gh, "o/ops", "runbook.yml", "main")


def test_dispatch_sends_runbook_params_and_correlation_id():
    fake = FakeActions()
    auto = actions(fake)
    cid = auto.start_job("Restart-Service", PARAMS)
    body = fake.dispatched[0]
    assert body["ref"] == "main" and body["inputs"]["runbook"] == "Restart-Service"
    assert json.loads(body["inputs"]["params"]) == PARAMS and body["inputs"]["correlation_id"] == cid


@pytest.mark.parametrize(
    ("lifecycle", "conclusion", "expected"),
    [
        (("queued",), None, "Running"),
        (("waiting",), None, "Running"),
        (("completed",), "success", "Completed"),
        (("completed",), "failure", "Failed"),
        (("completed",), "cancelled", "Failed"),
    ],
)
def test_job_status_maps_workflow_runs_to_automation_states(lifecycle, conclusion, expected):
    fake = FakeActions(lifecycle, conclusion)
    auto = actions(fake)
    cid = auto.start_job("Restart-Service", PARAMS)
    assert auto.job_status(cid) == expected


def test_status_is_running_before_the_run_exists():
    assert actions(FakeActions()).job_status("nope") == "Running"


# ---- GitHub-verified approver identity ---------------------------------------------------------
def verifier(user_status=200, repo_perms=None, repo_status=200, calls=None):
    def handler(req):
        if calls is not None:
            calls.append(req.url.path)
        if req.url.path == "/user":
            return httpx.Response(user_status, json={"login": "Alice"})
        return httpx.Response(repo_status, json={"permissions": repo_perms or {}})

    return GitHubIdentityVerifier(transport=httpx.MockTransport(handler))


def test_identity_requires_valid_token_visible_repo_and_write_access():
    assert verifier(repo_perms={"push": True}).verify("t", "o/r") == GitHubIdentity("Alice", True)
    assert verifier(repo_perms={"pull": True}).verify("t", "o/r") == GitHubIdentity("Alice", False)
    assert verifier(user_status=401).verify("t", "o/r") is None
    assert verifier(repo_status=404).verify("t", "o/r") is None


def test_identity_lookups_are_cached_per_token():
    calls = []
    v = verifier(repo_perms={"admin": True}, calls=calls)
    v.verify("t", "o/r")
    v.verify("t", "o/r")
    assert calls == ["/user", "/repos/o/r"]  # second call served from cache
    v.verify("other-token", "o/r")
    assert len(calls) == 4


class FakeVerifier:
    def __init__(self, ident):
        self.ident = ident

    def verify(self, token, repo):
        return self.ident if token == "good" else None


def approve(client, token=None):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return client.post("/admin/incidents/inc/approve", json={"approver": "mallory"}, headers=h)


def test_github_mode_approval_uses_verified_login_and_enforces_access(repo_root):
    c, client = _pending_incident(repo_root, admin_auth="github", incident_repo="o/r")
    client.app.state.identity_verifier = FakeVerifier(GitHubIdentity("alice", True))
    assert approve(client).status_code == 401  # no token
    assert approve(client, "bad").status_code == 401  # GitHub does not recognise it
    ok_ = approve(client, "good")
    assert ok_.status_code == 202
    ev = next(e for e in c.store.audit_events("inc") if e["event"] == "approval.requested")
    assert ev["actor"] == "alice" and ev["data"]["verified_identity"] is True  # body 'mallory' ignored


def test_github_mode_rejects_read_only_users_and_non_allow_listed_logins(repo_root):
    _, client = _pending_incident(repo_root, admin_auth="github", incident_repo="o/r")
    client.app.state.identity_verifier = FakeVerifier(GitHubIdentity("reader", False))
    assert approve(client, "good").status_code == 403
    _, client2 = _pending_incident(
        repo_root, admin_auth="github", incident_repo="o/r", approvers="bob, carol"
    )
    client2.app.state.identity_verifier = FakeVerifier(GitHubIdentity("alice", True))
    assert approve(client2, "good").status_code == 403
    client2.app.state.identity_verifier = FakeVerifier(GitHubIdentity("Carol", True))  # case-insensitive
    assert approve(client2, "good").status_code == 202


def test_github_mode_without_an_approver_repo_fails_closed(repo_root):
    _, client = _pending_incident(repo_root, admin_auth="github")
    client.app.state.identity_verifier = FakeVerifier(GitHubIdentity("alice", True))
    assert approve(client, "good").status_code == 503


# ---- Alertmanager endpoint ---------------------------------------------------------------------
@pytest.fixture
def am_client(repo_root):
    c = build_container(
        Settings(database_url="sqlite+pysqlite://", alert_token=SecretStr("tok")),
        backend=ScriptedBackend(replies=[]),
        base_dir=repo_root,
    )
    return c, TestClient(create_app(c))


def test_alertmanager_endpoint_auth_dedupe_and_filtering(am_client):
    c, client = am_client
    body = am_payload(am_alert("A"), am_alert("B"), am_alert("C", status="resolved"))
    assert client.post("/webhooks/alertmanager", json=body).status_code == 401
    assert (
        client.post(
            "/webhooks/alertmanager", json=body, headers={"authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    first = client.post("/webhooks/alertmanager", json=body, headers={"authorization": "Bearer tok"}).json()
    assert [j["correlated"] for j in first["jobs"]] == [False, False]  # resolved alert ignored
    again = client.post("/webhooks/alertmanager", json=body, headers={"x-alert-token": "tok"}).json()
    assert [j["correlated"] for j in again["jobs"]] == [True, True]  # repeat firing correlates
    only_resolved = am_payload(am_alert("C", status="resolved"))
    assert client.post(
        "/webhooks/alertmanager", json=only_resolved, headers={"x-alert-token": "tok"}
    ).json() == {"status": "ignored"}
    assert (
        client.post("/webhooks/alertmanager", content=b"{", headers={"x-alert-token": "tok"}).status_code
        == 400
    )


def test_alertmanager_endpoint_fails_closed_without_a_configured_token(repo_root):
    c = build_container(
        Settings(database_url="sqlite+pysqlite://"), backend=ScriptedBackend(replies=[]), base_dir=repo_root
    )
    r = TestClient(create_app(c)).post(
        "/webhooks/alertmanager", json=am_payload(am_alert()), headers={"authorization": "Bearer "}
    )
    assert r.status_code == 401


# ---- whole path: alert -> Prometheus evidence -> Actions runbook -> Prometheus verification -------
def run_free_path(repo_root, *, still_firing):
    fake = FakeActions()

    def prom_handler(req):
        q = req.url.params["query"]
        if q.startswith("ALERTS{") and fake.dispatched:  # recovery check, after the runbook ran
            return ok([{"metric": {}, "value": [0, "1"]}] if still_firing else [])
        return ok([{"metric": {"instance": "api-1:9100"}, "value": [0, "0.97"]}])  # evidence

    backend = ScriptedBackend(
        replies=[
            {
                "summary": "api saturated",
                "severity": "high",
                "confidence": 0.95,
                "recommended_runbook": "restart-service",
                "runbook_params": PARAMS,
                "hypotheses": [],
                "ticket_body": "",
            }
        ]
    )
    c = build_container(Settings(database_url="sqlite+pysqlite://"), backend=backend, base_dir=repo_root)
    llm = LLMGateway(backend, c.store, sleep=lambda s: None)
    agent = IncidentAgent(
        llm,
        PromptRegistry(repo_root / "prompts"),
        c.store,
        c.registry,
        PolicyEngine(PolicyConfig(), c.registry, c.store),
        PrometheusEvidence(prom(prom_handler)),
        actions(fake),
        PrometheusVerifier(prom(prom_handler)),
        LogNotifier(),
        sleep=lambda s: None,
    )
    c.handlers[AgentKind.INCIDENT] = agent.handle

    from devopsagents.integrations.azure.alerts import incident_revision, incident_target

    alert = parse_alertmanager(am_payload(am_alert(env="staging")))[0]
    job, _ = c.store.enqueue(
        AgentKind.INCIDENT,
        incident_target(alert),
        incident_revision(alert, "b"),
        "v1",
        {"mode": "analyse", "alert": alert.model_dump(mode="json")},
    )
    assert Worker(c, [AgentKind.INCIDENT]).tick() == {"succeeded": 1}
    return c.store.result(job.id), fake, backend


def test_free_incident_path_end_to_end(repo_root):
    res, fake, backend = run_free_path(repo_root, still_firing=False)
    assert res["status"] == "auto_resolved" and res["verified"] is True
    assert fake.dispatched[0]["inputs"]["runbook"] == "Restart-Service"
    # the model saw the Prometheus evidence and the alert annotations
    user = backend.calls[0]["user"]
    assert "prometheus: target up" in user and "summary: cpu high" in user


def test_alert_still_firing_after_the_runbook_is_escalated_not_reported_as_fixed(repo_root):
    res, fake, _ = run_free_path(repo_root, still_firing=True)
    assert len(fake.dispatched) == 1  # the action did run...
    assert res["status"] == "escalated" and res["verified"] is False  # ...but recovery was not confirmed


def test_container_picks_the_free_adapters_from_settings(repo_root):
    s = Settings(
        database_url="sqlite+pysqlite://",
        github_token=SecretStr("t"),
        prometheus_url="http://prom:9090",
        runbook_repo="o/ops",
    )
    agent = build_container(s, base_dir=repo_root).handlers[AgentKind.INCIDENT].__self__
    assert type(agent._evidence).__name__ == "PrometheusEvidence"
    assert type(agent._verifier).__name__ == "PrometheusVerifier"
    assert type(agent._auto).__name__ == "GitHubActionsAutomation"
