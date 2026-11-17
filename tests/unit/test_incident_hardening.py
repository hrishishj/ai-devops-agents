from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from devopsagents.agents.incident.workflow import IncidentAgent
from devopsagents.apps.api.main import create_app
from devopsagents.apps.container import LogNotifier, build_container
from devopsagents.apps.worker.main import Worker
from devopsagents.core.config.settings import Settings
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import LLMGateway
from devopsagents.core.policy.engine import PolicyConfig, PolicyEngine
from devopsagents.core.policy.runbooks import RunbookRegistry
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.actions import ActionRequest, Decision
from devopsagents.domain.events import AlertEvent
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.github.identity import GitHubIdentity
from tests.conftest import FakeClock
from tests.unit.test_incident import FakeAuto, FakeEvidence, FakeVerifier

DB = {"server_name": "db-1", "resource_group": "rg"}
SCALE = {"target_type": "ContainerApp", "target_name": "api", "resource_group": "rg1", "add_replicas": 2}


def analysis(runbook, params, conf=0.95):
    return {
        "summary": "s",
        "severity": "high",
        "confidence": conf,
        "hypotheses": [],
        "recommended_runbook": runbook,
        "runbook_params": params,
        "ticket_body": "",
    }


def alert(env="prod"):
    return AlertEvent(alert_id="a", source="x", title="db down", resource_id="/db", environment=env)


# ---- policy: two-person rule and approval TTL ---------------------------------------------------------
def test_required_approvals_depends_on_tier_and_environment(store, repo_root):
    reg = RunbookRegistry.load(repo_root / "runbooks")

    def need(runbook, env, **cfg):
        req = ActionRequest(runbook=runbook, params={}, environment=env, target="t", incident_id="i")
        return PolicyEngine(PolicyConfig(**cfg), reg, store).required_approvals(req)

    assert need("failover-db", "prod") == 2  # tier 2 in prod
    assert need("failover-db", "staging") == 1
    assert need("restart-service", "prod") == 1  # tier 1
    assert need("restart-service", "prod", two_person_min_tier=1) == 2  # configurable
    assert need("failover-db", "prod", two_person_environments=[]) == 1
    assert PolicyEngine(PolicyConfig(approval_ttl_minutes=5), reg, store).approval_ttl_seconds == 300


def test_rollback_is_exempt_from_cooldown_but_nothing_else(store, repo_root):
    reg = RunbookRegistry.load(repo_root / "runbooks")
    eng = PolicyEngine(PolicyConfig(), reg, store)
    params = {
        "target_type": "ContainerApp",
        "target_name": "api",
        "resource_group": "rg",
        "remove_replicas": 1,
    }
    req = ActionRequest(
        runbook="scale-in",
        params=params,
        environment="staging",
        target="t",
        incident_id="i",
        model_confidence=1.0,
    )
    store.audit("agent", "action.executed", "t")  # the action being undone just ran on this target
    assert eng.evaluate(req, evidence=["e"]).decision == Decision.DENY  # normal actions hit the cooldown
    assert eng.evaluate(req, evidence=["e"], is_rollback=True).decision == Decision.ALLOW
    assert (
        PolicyEngine(PolicyConfig(kill_switch=True), reg, store)
        .evaluate(req, evidence=["e"], is_rollback=True)
        .decision
        == Decision.DENY
    )  # kill switch still wins
    prod = req.model_copy(update={"environment": "prod"})
    assert eng.evaluate(prod, evidence=["e"], is_rollback=True).decision == Decision.REQUIRE_APPROVAL


# ---- runbook rollback mapping -------------------------------------------------------------------------
def test_rollback_params_keep_types_and_format_strings(repo_root):
    reg = RunbookRegistry.load(repo_root / "runbooks")
    out = reg.build_rollback_params("scale-out", SCALE)
    assert out == {
        "target_type": "ContainerApp",
        "target_name": "api",
        "resource_group": "rg1",
        "remove_replicas": 2,
    }
    assert isinstance(out["remove_replicas"], int)  # "{add_replicas}" copies the integer, not "2"
    assert reg.build_rollback_params("scale-out", {"target_name": "api"}) is None  # missing value
    assert reg.build_rollback_params("restart-service", {}) is None  # no rollback defined


def write_runbooks(tmp_path, repo_root, scale_out_extra="", scale_in_extra=""):
    for name, extra in (("scale-out", scale_out_extra), ("scale-in", scale_in_extra)):
        d = tmp_path / name
        d.mkdir(parents=True)
        shutil.copy(repo_root / "runbooks" / name / "runbook.yaml", d / "runbook.yaml")
        if extra:
            with open(d / "runbook.yaml", "a", encoding="utf-8") as fh:
                fh.write("\n" + extra + "\n")
    return tmp_path


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("auto_rollback: true\nrollback_params: {}\nrollback: scale-in", "need both"),
        ("rollback_params:\n  bogus: x", "not a parameter of scale-in"),
        ("rollback_params:\n  remove_replicas: '{nope}'", "unknown parameter 'nope'"),
    ],
)
def test_registry_rejects_inconsistent_rollback_definitions(tmp_path, repo_root, extra, message):
    # override the shipped mapping by re-declaring keys at the end of the YAML (last key wins)
    root = write_runbooks(tmp_path, repo_root, scale_out_extra=extra)
    with pytest.raises(ValueError, match=message):
        RunbookRegistry.load(root)


# ---- agent: pending approvals carry expiry and requirements ----------------------------------------------
def build_rig(repo_root, replies, *, runbooks_dir=None, verifier=None, settings=None, env="prod"):
    s = Settings(database_url="sqlite+pysqlite://", **(settings or {}))
    backend = ScriptedBackend(replies=replies)
    c = build_container(s, backend=backend, base_dir=repo_root)
    clock = FakeClock()
    c.store._clock = clock
    reg = RunbookRegistry.load(runbooks_dir or repo_root / "runbooks")
    auto = FakeAuto()
    llm = LLMGateway(backend, c.store, sleep=lambda x: None)
    agent = IncidentAgent(
        llm,
        PromptRegistry(repo_root / "prompts"),
        c.store,
        reg,
        PolicyEngine(PolicyConfig(), reg, c.store),
        FakeEvidence(),
        auto,
        verifier or FakeVerifier(True),
        LogNotifier(),
        sleep=lambda x: None,
    )
    c.handlers[AgentKind.INCIDENT] = agent.handle
    job, _ = c.store.enqueue(
        AgentKind.INCIDENT,
        "inc",
        "r1",
        "v1",
        {"mode": "analyse", "alert": alert(env).model_dump(mode="json")},
    )
    assert Worker(c, [AgentKind.INCIDENT]).tick() == {"succeeded": 1}
    return c, auto, clock, c.store.result(job.id)


def test_pending_approval_records_expiry_and_required_approvals(repo_root):
    c, _, clock, res = build_rig(repo_root, [analysis("failover-db", DB)])
    assert res["status"] == "awaiting_approval" and res["required_approvals"] == 2
    assert res["expires_at"] == pytest.approx(clock() + 120 * 60)
    _, _, _, one = build_rig(
        repo_root,
        [
            analysis(
                "restart-service",
                {"service_type": "ContainerApp", "service_name": "api", "resource_group": "rg"},
            )
        ],
    )
    assert one["required_approvals"] == 1


# ---- approval endpoint: two verified approvers -------------------------------------------------------------
class Tokens:
    """Maps a bearer token to a GitHub identity."""

    def __init__(self, mapping):
        self.mapping = mapping

    def verify(self, token, repo):
        return self.mapping.get(token)


def vote(client, token):
    return client.post("/admin/incidents/inc/approve", headers={"authorization": f"Bearer {token}"})


def github_client(c):
    client = TestClient(create_app(c))
    client.app.state.identity_verifier = Tokens(
        {
            "ta": GitHubIdentity("alice", True),
            "tb": GitHubIdentity("bob", True),
            "ta2": GitHubIdentity("Alice", True),
        }
    )
    return client


def test_two_distinct_verified_approvers_are_required_to_execute(repo_root):
    c, auto, _, _ = build_rig(
        repo_root, [analysis("failover-db", DB)], settings={"admin_auth": "github", "incident_repo": "o/r"}
    )
    client = github_client(c)
    first = vote(client, "ta")
    assert first.status_code == 202 and first.json() == {
        "status": "pending_more_approvals",
        "approvals": 1,
        "required": 2,
        "approvers": ["alice"],
    }
    assert Worker(c, [AgentKind.INCIDENT]).tick() == {}  # nothing runs after one vote
    again = vote(client, "ta2")  # same person (case-insensitive) cannot vote twice
    assert again.json()["approvals"] == 1 and auto.started == []
    done = vote(client, "tb")
    assert done.json()["status"] == "accepted" and done.json()["approvals"] == 2
    assert Worker(c, [AgentKind.INCIDENT]).tick() == {"succeeded": 1}
    assert [s[0] for s in auto.started] == ["Failover-Database"]
    ex = next(e for e in c.store.audit_events("/db") if e["event"] == "action.executed")
    assert ex["data"]["approved_by"] == "alice, bob"
    assert vote(client, "tb").json()["status"] == "already_approved"  # idempotent


def test_two_person_actions_cannot_be_approved_with_self_declared_names(repo_root):
    c, auto, _, _ = build_rig(
        repo_root, [analysis("failover-db", DB)], settings={"admin_token": "adm"}
    )  # default token mode
    client = TestClient(create_app(c))
    for name in ("a", "b"):
        r = client.post(
            "/admin/incidents/inc/approve", json={"approver": name}, headers={"x-admin-token": "adm"}
        )
        assert r.status_code == 403 and "two verified approvers" in r.json()["detail"]
    assert auto.started == []


def test_single_approver_actions_still_work_in_token_mode(repo_root):
    c, auto, _, _ = build_rig(
        repo_root,
        [
            analysis(
                "restart-service",
                {"service_type": "ContainerApp", "service_name": "api", "resource_group": "rg"},
            )
        ],
        settings={"admin_token": "adm"},
    )
    client = TestClient(create_app(c))
    r = client.post(
        "/admin/incidents/inc/approve", json={"approver": "alice"}, headers={"x-admin-token": "adm"}
    )
    assert r.status_code == 202 and r.json()["approvals"] == 1
    assert Worker(c, [AgentKind.INCIDENT]).tick() == {"succeeded": 1} and len(auto.started) == 1


# ---- approval expiry --------------------------------------------------------------------------------------------
def test_expired_approvals_are_rejected_at_the_endpoint(repo_root):
    c, auto, clock, _ = build_rig(
        repo_root, [analysis("failover-db", DB)], settings={"admin_auth": "github", "incident_repo": "o/r"}
    )
    client = github_client(c)
    assert vote(client, "ta").status_code == 202  # still valid
    clock.advance(120 * 60 + 1)
    r = vote(client, "tb")
    assert r.status_code == 410 and "expired" in r.json()["detail"]
    assert auto.started == []


def test_an_approval_that_expires_before_execution_is_denied_by_the_agent(repo_root):
    c, auto, clock, res = build_rig(repo_root, [analysis("failover-db", DB)])
    j, _ = c.store.enqueue(
        AgentKind.INCIDENT,
        "inc",
        "approve-x",
        "v1",
        {
            "mode": "execute_approved",
            "approver": "alice, bob",
            "action": res["pending_action"],
            "alert": res["alert"],
            "evidence": res["evidence"],
            "expires_at": clock() + 10,
        },
    )
    clock.advance(11)  # the worker picks it up too late
    assert Worker(c, [AgentKind.INCIDENT]).tick() == {"succeeded": 1}
    assert c.store.result(j.id) == {"status": "denied", "reasons": ["approval expired"]}
    assert auto.started == [] and any(e["event"] == "approval.expired" for e in c.store.audit_events("inc"))


# ---- automatic rollback --------------------------------------------------------------------------------------------
def test_automatic_rollback_runs_scale_in_with_derived_parameters(repo_root, tmp_path):
    root = write_runbooks(tmp_path, repo_root, scale_out_extra="auto_rollback: true")
    c, auto, _, res = build_rig(
        repo_root,
        [analysis("scale-out", SCALE)],
        runbooks_dir=root,
        verifier=FakeVerifier(False),
        env="staging",
    )
    assert res["status"] == "escalated" and res["rollback"]["attempted"] is True
    assert [s[0] for s in auto.started] == ["Scale-Out", "Scale-In"]
    assert auto.started[1][1] == {
        "target_type": "ContainerApp",
        "target_name": "api",
        "resource_group": "rg1",
        "remove_replicas": 2,
    }
    events = [e["event"] for e in c.store.audit_events()]
    assert "policy.rollback.allow" in events
    rb = [e for e in c.store.audit_events("/db") if e["event"] == "action.executed"][-1]
    assert rb["data"]["rollback_of"] == "scale-out"


def test_rollback_is_off_by_default_and_never_runs_when_recovery_is_unknown(repo_root, tmp_path):
    _, auto, _, res = build_rig(
        repo_root, [analysis("scale-out", SCALE)], verifier=FakeVerifier(False), env="staging"
    )  # shipped scale-out: auto_rollback not set
    assert res["rollback"] is None and [s[0] for s in auto.started] == ["Scale-Out"]
    root = write_runbooks(tmp_path, repo_root, scale_out_extra="auto_rollback: true")
    _, auto2, _, res2 = build_rig(
        repo_root,
        [analysis("scale-out", SCALE)],
        runbooks_dir=root,
        verifier=FakeVerifier(None),
        env="staging",
    )
    assert res2["rollback"] is None and len(auto2.started) == 1  # unknown != failed: do not guess


def test_in_prod_the_action_waits_for_approval_so_no_rollback_can_happen_first(repo_root, tmp_path):
    root = write_runbooks(tmp_path, repo_root, scale_out_extra="auto_rollback: true")
    c, auto, _, res = build_rig(
        repo_root,
        [analysis("scale-out", SCALE)],
        runbooks_dir=root,
        verifier=FakeVerifier(False),
        env="staging",
    )
    assert res["rollback"]["attempted"] is True  # staging is an auto environment
    c2, auto2, _, pending = build_rig(
        repo_root, [analysis("scale-out", SCALE)], runbooks_dir=root, verifier=FakeVerifier(False), env="prod"
    )
    assert (
        pending["status"] == "awaiting_approval" and auto2.started == []
    )  # the action itself needs approval first
