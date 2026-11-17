from __future__ import annotations

import pytest

from devopsagents.agents.incident.workflow import IncidentAgent
from devopsagents.core.jobs.runner import Outcome, run_job
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import LLMGateway
from devopsagents.core.policy.engine import PolicyConfig, PolicyEngine
from devopsagents.core.policy.runbooks import RunbookRegistry
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.actions import ActionRequest
from devopsagents.domain.events import AlertEvent
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.azure.evidence import Evidence

PARAMS = {"service_type": "ContainerApp", "service_name": "api", "resource_group": "rg1"}


class FakeEvidence:
    def __init__(self, items=("cpu 98% for 10m",)):
        self.items = items

    def gather(self, alert):
        return [Evidence("kql", "q", s) for s in self.items]


class FakeAuto:
    def __init__(self, final="Completed"):
        self.started, self.final = [], final

    def start_job(self, runbook, params):
        self.started.append((runbook, params))
        return "job-1"

    def job_status(self, job_id):
        return self.final


class FakeVerifier:
    def __init__(self, v=True):
        self.v = v

    def healthy(self, alert):
        return self.v


class FakeNotifier:
    def __init__(self):
        self.tickets = []

    def notify(self, title, body, labels):
        self.tickets.append((title, body, labels))
        return "https://example/issues/1"


def analysis(runbook="restart-service", params=None, conf=0.95):
    return {
        "summary": "api saturated",
        "severity": "high",
        "confidence": conf,
        "hypotheses": [{"cause": "cpu saturation", "likelihood": 0.9, "evidence_ids": [0]}],
        "recommended_runbook": runbook,
        "runbook_params": params or PARAMS,
        "ticket_body": "check dashboards",
    }


def alert(env="staging"):
    return AlertEvent(
        alert_id="a1",
        source="azure_monitor",
        title="High CPU",
        severity="sev2",
        resource_id="/subs/x/api",
        environment=env,
    )


@pytest.fixture
def rig(store, repo_root):
    def build(replies, *, env="staging", auto=None, verifier=None, evidence=None, **cfg):
        reg = RunbookRegistry.load(repo_root / "runbooks")
        llm = LLMGateway(ScriptedBackend(replies=replies), store, sleep=lambda s: None)
        engine = PolicyEngine(PolicyConfig(**cfg), reg, store)
        notifier, auto = FakeNotifier(), auto or FakeAuto()
        agent = IncidentAgent(
            llm,
            PromptRegistry(repo_root / "prompts"),
            store,
            reg,
            engine,
            evidence or FakeEvidence(),
            auto,
            verifier or FakeVerifier(True),
            notifier,
            sleep=lambda s: None,
        )
        job, _ = store.enqueue(
            AgentKind.INCIDENT, "inc-1", "r1", "v1", {"alert": alert(env).model_dump(mode="json")}
        )
        return agent, job, auto, notifier

    return build


def run(store, agent, job):
    out = run_job(store, job.id, {AgentKind.INCIDENT: agent.handle})
    return out, store.result(job.id)


def test_tier1_in_staging_auto_resolves_and_audits(store, rig):
    agent, job, auto, notifier = rig([analysis()])
    out, res = run(store, agent, job)
    assert out == Outcome.SUCCEEDED and res["status"] == "auto_resolved" and res["verified"]
    assert auto.started == [("Restart-Service", PARAMS)]
    events = [e["event"] for e in store.audit_events()]
    assert "action.executed" in events and "action.verified" in events
    assert notifier.tickets == []  # nothing to escalate


def test_prod_requires_approval_and_does_not_execute(store, rig):
    agent, job, auto, notifier = rig([analysis()], env="prod")
    _, res = run(store, agent, job)
    assert res["status"] == "awaiting_approval" and auto.started == []
    assert res["pending_action"]["runbook"] == "restart-service"
    assert len(notifier.tickets) == 1 and "Approval required" in notifier.tickets[0][1]


def test_no_evidence_denies_even_with_high_confidence(store, rig):
    agent, job, auto, _ = rig([analysis(conf=1.0)], evidence=FakeEvidence(items=()))
    _, res = run(store, agent, job)
    assert res["status"] == "denied" and auto.started == []


def test_hallucinated_runbook_or_params_never_execute(store, rig):
    agent, job, auto, _ = rig([analysis(runbook="drop-database")])
    _, res = run(store, agent, job)
    assert res["status"] == "denied" and auto.started == []

    agent2, job2, auto2, _ = rig([analysis(params={**PARAMS, "service_name": "x; reboot"})])
    store.cancel(job.id)
    # distinct incident for the second job
    j, _ = store.enqueue(AgentKind.INCIDENT, "inc-2", "r1", "v1", {"alert": alert().model_dump(mode="json")})
    _, res2 = run(store, agent2, j)
    assert res2["status"] == "denied" and auto2.started == []


def test_null_runbook_just_tickets(store, rig):
    agent, job, auto, notifier = rig([analysis(runbook=None)])
    _, res = run(store, agent, job)
    assert res["status"] == "ticketed" and auto.started == [] and len(notifier.tickets) == 1


def test_failed_verification_escalates_with_rollback_hint(store, rig):
    agent, job, auto, notifier = rig(
        [
            analysis(
                runbook="scale-out",
                params={
                    "target_type": "ContainerApp",
                    "target_name": "api",
                    "resource_group": "rg1",
                    "add_replicas": 1,
                },
            )
        ],
        verifier=FakeVerifier(False),
    )
    _, res = run(store, agent, job)
    assert res["status"] == "escalated" and "scale-in" in notifier.tickets[0][1]


def test_unverifiable_recovery_is_not_reported_as_success(store, rig):
    agent, job, _, notifier = rig([analysis()], verifier=FakeVerifier(None))
    _, res = run(store, agent, job)
    assert res["status"] == "escalated" and "could not be verified" in notifier.tickets[0][1]


def test_automation_failure_escalates(store, rig):
    agent, job, _, notifier = rig([analysis()], auto=FakeAuto(final="Failed"))
    _, res = run(store, agent, job)
    assert res["status"] == "action_failed" and notifier.tickets


def test_cooldown_blocks_repeat_action(store, rig):
    agent, job, auto, _ = rig([analysis()])
    run(store, agent, job)
    j2, _ = store.enqueue(AgentKind.INCIDENT, "inc-9", "r1", "v1", {"alert": alert().model_dump(mode="json")})
    agent2, _, _, _ = rig([analysis()])
    _, res = run(store, agent2, j2)
    assert res["status"] == "denied" and "cooldown" in res["reasons"][0]


def test_approval_executes_but_cannot_override_a_denial(store, rig):
    agent, job, auto, _ = rig([analysis()], env="prod")
    _, pending = run(store, agent, job)
    action = pending["pending_action"]

    approve, _ = store.enqueue(
        AgentKind.INCIDENT,
        "inc-1",
        "approve-alice",
        "v1",
        {
            "mode": "execute_approved",
            "approver": "alice",
            "action": action,
            "alert": alert("prod").model_dump(mode="json"),
            "evidence": ["cpu 98%"],
        },
    )
    _, res = run(store, agent, approve)
    assert res["status"] == "resolved_after_approval" and len(auto.started) == 1
    ev = [e for e in store.audit_events() if e["event"] == "action.executed"][0]
    assert ev["actor"] == "alice" and ev["data"]["approved_by"] == "alice"

    # Same action approved again immediately: cooldown is a DENY that approval cannot lift.
    again, _ = store.enqueue(
        AgentKind.INCIDENT,
        "inc-1",
        "approve-bob",
        "v1",
        {
            "mode": "execute_approved",
            "approver": "bob",
            "action": action,
            "alert": alert("prod").model_dump(mode="json"),
            "evidence": ["cpu 98%"],
        },
    )
    _, res2 = run(store, agent, again)
    assert res2["status"] == "denied" and len(auto.started) == 1


def test_kill_switch_blocks_approved_actions(store, rig):
    agent, job, auto, _ = rig([analysis()], env="prod", kill_switch=True)
    req = ActionRequest(
        runbook="restart-service", params=PARAMS, environment="prod", target="t", incident_id="inc-1"
    )
    j, _ = store.enqueue(
        AgentKind.INCIDENT,
        "inc-1",
        "approve-x",
        "v1",
        {
            "mode": "execute_approved",
            "approver": "alice",
            "action": req.model_dump(mode="json"),
            "alert": alert("prod").model_dump(mode="json"),
            "evidence": ["e"],
        },
    )
    _, res = run(store, agent, j)
    assert res["status"] == "denied" and auto.started == []
