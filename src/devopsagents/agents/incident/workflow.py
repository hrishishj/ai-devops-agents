"""Incident workflow: evidence -> hypotheses -> policy -> (execute | request approval | ticket)
-> verify. The model proposes; the policy engine decides; every side effect is audited."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field

from devopsagents.core.jobs.store import JobStore
from devopsagents.core.llm.gateway import LLMGateway, ModelTier
from devopsagents.core.policy.engine import PolicyEngine
from devopsagents.core.policy.runbooks import RunbookRegistry
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.core.telemetry.tracing import span
from devopsagents.domain.actions import ActionRequest, Decision
from devopsagents.domain.events import AlertEvent
from devopsagents.domain.jobs import Job
from devopsagents.integrations.azure.automation import TERMINAL, AutomationPort
from devopsagents.integrations.azure.evidence import EvidenceSource, Verifier

log = logging.getLogger(__name__)

AGENT_VERSION = "incident-1"


class Hypothesis(BaseModel):
    cause: str
    likelihood: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[int] = Field(default_factory=list)


class IncidentAnalysis(BaseModel):
    summary: str
    severity: str = "medium"
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommended_runbook: str | None = None
    runbook_params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ticket_body: str = ""


class Notifier(Protocol):
    def notify(self, title: str, body: str, labels: list[str]) -> str: ...


class IncidentAgent:
    def __init__(
        self,
        gateway: LLMGateway,
        prompts: PromptRegistry,
        store: JobStore,
        registry: RunbookRegistry,
        policy: PolicyEngine,
        evidence: EvidenceSource,
        automation: AutomationPort,
        verifier: Verifier,
        notifier: Notifier,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 10.0,
        max_wait_seconds: int = 600,
    ) -> None:
        self._llm, self._prompts, self._store = gateway, prompts, store
        self._reg, self._policy, self._evidence = registry, policy, evidence
        self._auto, self._verifier, self._notify = automation, verifier, notifier
        self._sleep, self._poll, self._max_wait = sleep, poll_seconds, max_wait_seconds

    # ------------------------------------------------------------------------------------
    def handle(self, job: Job) -> dict[str, Any]:
        mode = job.payload.get("mode", "analyse")
        if mode == "execute_approved":
            return self._execute_approved(job)
        return self._analyse(job)

    def _analyse(self, job: Job) -> dict[str, Any]:
        alert = AlertEvent.model_validate(job.payload["alert"])
        incident = job.target
        with span("incident.analyse", incident=incident, env=alert.environment):
            ev = self._evidence.gather(alert)
            ev_lines = [e.render() for e in ev]
            prompt = self._prompts.get("incident", "analyse")
            analysis = self._llm.structured(
                tier=ModelTier.LARGE,
                system=prompt.text,
                user=_user_message(alert, ev_lines),
                response_model=IncidentAnalysis,
                scope="incidents",
                prompt_id=prompt.id,
                max_tokens=2500,
            )
            self._store.audit(
                "incident",
                "incident.analysed",
                incident,
                {"confidence": analysis.confidence, "runbook": analysis.recommended_runbook},
            )

            if not analysis.recommended_runbook:
                url = self._ticket(alert, analysis, ev_lines, "No safe automated remediation proposed.")
                return {"status": "ticketed", "ticket": url, "summary": analysis.summary}

            req = ActionRequest(
                runbook=analysis.recommended_runbook,
                params=analysis.runbook_params,
                environment=alert.environment,
                target=alert.resource_id or alert.fingerprint,
                incident_id=incident,
                severity=analysis.severity,
                rationale=analysis.summary,
                model_confidence=analysis.confidence,
            )
            decision = self._policy.evaluate(req, evidence=ev_lines)
            self._store.audit(
                "policy",
                f"policy.{decision.decision.value}",
                incident,
                {"runbook": req.runbook, "reasons": decision.reasons},
            )

            if decision.decision == Decision.ALLOW:
                return self._execute(job, alert, analysis, req, ev_lines, approver=None)

            note = (
                "Approval required: "
                if decision.decision == Decision.REQUIRE_APPROVAL
                else "Automation denied: "
            ) + "; ".join(decision.reasons)
            url = self._ticket(alert, analysis, ev_lines, note, proposed=req)
            return {
                "status": "awaiting_approval" if decision.decision == Decision.REQUIRE_APPROVAL else "denied",
                "reasons": decision.reasons,
                "ticket": url,
                "pending_action": req.model_dump(mode="json")
                if decision.decision == Decision.REQUIRE_APPROVAL
                else None,
                "alert": alert.model_dump(mode="json"),
                "evidence": ev_lines,
            }

    def _execute_approved(self, job: Job) -> dict[str, Any]:
        """A human approved a previously proposed action. Policy is re-checked: approval can
        lift a REQUIRE_APPROVAL, but never a DENY (cooldown, bad params, kill switch...)."""
        req = ActionRequest.model_validate(job.payload["action"])
        approver = str(job.payload["approver"])
        alert = AlertEvent.model_validate(job.payload["alert"])
        raw_ev = job.payload.get("evidence")
        ev_lines = [str(x) for x in raw_ev] if isinstance(raw_ev, list) else []
        decision = self._policy.evaluate(req, evidence=ev_lines)
        self._store.audit(
            approver,
            "approval.granted",
            req.incident_id,
            {"runbook": req.runbook, "policy": decision.decision.value},
        )
        if decision.decision == Decision.DENY:
            return {"status": "denied", "reasons": decision.reasons}
        analysis = IncidentAnalysis(
            summary=req.rationale,
            severity=req.severity,
            recommended_runbook=req.runbook,
            runbook_params=req.params,
        )
        return self._execute(job, alert, analysis, req, ev_lines, approver=approver)

    # ------------------------------------------------------------------------------------
    def _execute(
        self,
        job: Job,
        alert: AlertEvent,
        analysis: IncidentAnalysis,
        req: ActionRequest,
        ev_lines: list[str],
        approver: str | None,
    ) -> dict[str, Any]:
        if not self._store.is_current(job.id):
            return {"status": "dropped", "reason": "superseded"}
        spec = self._reg.get(req.runbook)
        assert spec is not None  # policy already confirmed it exists
        # Audit BEFORE the side effect so a crash can never lead to an untracked action.
        self._store.audit(
            approver or "agent",
            "action.executed",
            req.target,
            {
                "runbook": req.runbook,
                "params": req.params,
                "incident": req.incident_id,
                "approved_by": approver,
            },
        )
        auto_job = self._auto.start_job(spec.automation_runbook, req.params)

        waited, state = 0.0, "Running"
        while waited <= self._max_wait:
            state = self._auto.job_status(auto_job)
            if state in TERMINAL:
                break
            self._sleep(self._poll)
            waited += self._poll
        if state != "Completed":
            note = f"Runbook ended in state {state!r} (job {auto_job})."
            url = self._ticket(alert, analysis, ev_lines, note, proposed=req)
            return {"status": "action_failed", "automation_job": auto_job, "state": state, "ticket": url}

        self._sleep(min(spec.verification.wait_seconds, self._max_wait))
        healthy = self._verifier.healthy(alert)
        self._store.audit(
            "agent", "action.verified", req.target, {"runbook": req.runbook, "healthy": healthy}
        )
        if healthy:
            return {
                "status": "auto_resolved" if approver is None else "resolved_after_approval",
                "runbook": req.runbook,
                "automation_job": auto_job,
                "verified": True,
            }

        rollback = f" Rollback runbook available: {spec.rollback}." if spec.rollback else ""
        reason = "still failing" if healthy is False else "could not be verified"
        url = self._ticket(
            alert,
            analysis,
            ev_lines,
            f"Action ran but recovery {reason}.{rollback} Paging on-call.",
            proposed=req,
        )
        return {"status": "escalated", "verified": healthy, "automation_job": auto_job, "ticket": url}

    def _ticket(
        self,
        alert: AlertEvent,
        a: IncidentAnalysis,
        ev_lines: list[str],
        note: str,
        proposed: ActionRequest | None = None,
    ) -> str:
        parts = [
            f"## {a.summary}",
            f"**Environment:** {alert.environment} · **Resource:** `{alert.resource_id}`",
            f"**Model confidence:** {a.confidence:.2f} (advisory)",
            f"\n> {note}",
        ]
        if a.hypotheses:
            parts.append("\n### Hypotheses")
            parts += [f"- ({h.likelihood:.2f}) {h.cause} — evidence {h.evidence_ids}" for h in a.hypotheses]
        if ev_lines:
            parts.append("\n### Evidence")
            parts += [f"{i}. {line}" for i, line in enumerate(ev_lines)]
        if proposed:
            parts.append(f"\n### Proposed action\n`{proposed.runbook}` `{proposed.params}`")
        if a.ticket_body:
            parts.append(f"\n### Details\n{a.ticket_body}")
        return self._notify.notify(
            f"[{a.severity.upper()}] {alert.title}", "\n".join(parts), ["incident", "ai-triage", a.severity]
        )


def _user_message(alert: AlertEvent, evidence: list[str]) -> str:
    ev = "\n".join(f"[{i}] {e}" for i, e in enumerate(evidence)) or "(none collected)"
    runbooks = ""
    return (
        f"<alert>\ntitle: {alert.title}\nseverity: {alert.severity}\nresource: {alert.resource_id}\n"
        f"environment: {alert.environment}\nfired_at: {alert.fired_at}\n</alert>\n"
        f"<evidence>\n{ev}\n</evidence>{runbooks}"
    )
