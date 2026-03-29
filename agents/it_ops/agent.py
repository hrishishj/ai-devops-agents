"""
IT Ops Agent
============
Ingests Azure Monitor alerts and system log payloads, identifies root-cause
patterns with GPT-4o, and either:
  - Auto-resolves by triggering an Azure Automation runbook (confidence >= threshold)
  - Drafts a support ticket response for human review          (confidence <  threshold)

Pipeline
--------
  1. Log Analyser     — regex + embedding pattern extraction
  2. LLM Analysis     — GPT-4o root-cause + remediation JSON
  3. Decision Router  — confidence threshold gate
  4. Runbook Executor — Azure Automation REST API call
  5. Ticket Drafter   — formats human-readable response
  6. Cosmos Persist   — stores incident record
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import requests
from azure.identity import DefaultAzureCredential
from pydantic import BaseModel, ValidationError, field_validator

from shared.config import get_settings
from shared.cosmos_store import CosmosMemoryStore
from shared.llm_client import LLMRequest, get_llm_client
from shared.redis_cache import get_cache
from shared.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)
_settings = get_settings()


# --------------------------------------------------------------------------- #
# Enumerations & Schemas
# --------------------------------------------------------------------------- #

class IncidentOutcome(str, Enum):
    AUTO_RESOLVED = "auto_resolved"
    ESCALATED = "escalated"
    DRAFT_CREATED = "draft_created"


class RemediationAction(BaseModel):
    runbook_name: str | None = None
    parameters: dict[str, Any] = {}
    description: str


class ItOpsAnalysis(BaseModel):
    incident_summary: str
    root_cause: str
    affected_components: list[str]
    severity: str              # "critical" | "high" | "medium" | "low"
    confidence: float          # 0.0 – 1.0
    remediation: RemediationAction
    ticket_draft: str          # human-readable ticket body

    @field_validator("severity")
    @classmethod
    def valid_severity(cls, v: str) -> str:
        allowed = {"critical", "high", "medium", "low"}
        if v not in allowed:
            raise ValueError(f"severity must be one of {allowed}")
        return v

    @field_validator("confidence")
    @classmethod
    def valid_confidence(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v


# --------------------------------------------------------------------------- #
# Log Analyser
# --------------------------------------------------------------------------- #

# Common error signatures to extract before LLM processing
_ERROR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("OutOfMemory",         re.compile(r"out.?of.?memory|OOMKilled", re.IGNORECASE)),
    ("ConnectionTimeout",   re.compile(r"connection.?timed?.?out|ETIMEDOUT", re.IGNORECASE)),
    ("DiskFull",            re.compile(r"no space left on device|disk full|ENOSPC", re.IGNORECASE)),
    ("HighCPU",             re.compile(r"cpu.?usage.{0,20}(9[0-9]|100)\s*%", re.IGNORECASE)),
    ("NullPointer",         re.compile(r"NullPointerException|null reference|NoneType", re.IGNORECASE)),
    ("AuthFailure",         re.compile(r"401 Unauthorized|403 Forbidden|auth.?fail", re.IGNORECASE)),
    ("ServiceUnavailable",  re.compile(r"503 Service Unavailable|service.?unavailable", re.IGNORECASE)),
    ("DatabaseError",       re.compile(r"deadlock|connection.?pool.?exhaust|too many connections", re.IGNORECASE)),
]


def extract_error_signatures(log_text: str) -> list[str]:
    """Return a list of matched error pattern names from log text."""
    matches = [name for name, pattern in _ERROR_PATTERNS if pattern.search(log_text)]
    return matches


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #

@dataclass
class ItOpsAgent:
    """
    Analyses system log payloads and either auto-resolves or drafts
    a human-readable incident ticket.
    """

    _llm = None
    _store: CosmosMemoryStore = field(
        default_factory=lambda: CosmosMemoryStore(get_settings().cosmos_container_incidents)
    )
    _cache = None

    def __post_init__(self) -> None:
        self._llm = get_llm_client()
        self._cache = get_cache()

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def handle_alert(
        self,
        alert_id: str,
        source: str,
        log_payload: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Main entry point for an incoming monitoring alert.

        Parameters
        ----------
        alert_id  : Unique ID from Azure Monitor / Service Bus message ID
        source    : Origin system (e.g. "azure_monitor", "application_logs")
        log_payload : Raw log text or JSON-serialised alert payload
        metadata  : Optional extra context (subscription, resource group, etc.)

        Returns a result dict with outcome and ticket/runbook details.
        """
        idempotency_key = f"it_ops:{alert_id}"
        if not self._cache.claim_idempotency_key(idempotency_key):
            logger.info("Duplicate alert '%s' — skipping.", alert_id)
            return {"outcome": "duplicate", "alert_id": alert_id}

        with tracer.start_as_current_span("it_ops.handle_alert") as span:
            span.set_attribute("alert.id", alert_id)
            span.set_attribute("alert.source", source)

            # 1. Extract error signatures
            signatures = extract_error_signatures(log_payload)
            span.set_attribute("alert.signatures", ",".join(signatures))

            # 2. LLM analysis
            analysis = self._analyse_with_llm(log_payload, signatures, metadata or {})
            if analysis is None:
                return {"outcome": "error", "alert_id": alert_id, "reason": "llm_parse_failure"}

            span.set_attribute("alert.confidence", analysis.confidence)
            span.set_attribute("alert.severity", analysis.severity)

            # 3. Route
            outcome = self._route(alert_id, analysis)
            span.set_attribute("alert.outcome", outcome.value)

            # 4. Persist
            record = self._persist(alert_id, source, log_payload, analysis, outcome, metadata)
            return {
                "outcome": outcome.value,
                "alert_id": alert_id,
                "incident_id": record["id"],
                "confidence": analysis.confidence,
                "severity": analysis.severity,
                "ticket_draft": analysis.ticket_draft,
                "runbook_name": analysis.remediation.runbook_name,
            }

    # ------------------------------------------------------------------ #
    # Pipeline stages
    # ------------------------------------------------------------------ #

    def _analyse_with_llm(
        self,
        log_payload: str,
        signatures: list[str],
        metadata: dict[str, Any],
    ) -> ItOpsAnalysis | None:
        user_msg = _build_itops_user_message(log_payload, signatures, metadata)
        request = LLMRequest(
            system_prompt=_ITOPS_SYSTEM_PROMPT,
            user_message=user_msg,
            temperature=0.0,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        raw = self._llm.complete(request).content
        try:
            data = json.loads(raw)
            return ItOpsAnalysis(**data)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.error("IT Ops LLM parse failure: %s\nRaw: %s", exc, raw[:500])
            return None

    def _route(self, alert_id: str, analysis: ItOpsAnalysis) -> IncidentOutcome:
        threshold = _settings.confidence_threshold_auto_resolve
        if analysis.confidence >= threshold and analysis.remediation.runbook_name:
            success = self._execute_runbook(
                runbook_name=analysis.remediation.runbook_name,
                parameters=analysis.remediation.parameters,
                alert_id=alert_id,
            )
            return IncidentOutcome.AUTO_RESOLVED if success else IncidentOutcome.ESCALATED
        return IncidentOutcome.DRAFT_CREATED

    def _execute_runbook(
        self,
        runbook_name: str,
        parameters: dict[str, Any],
        alert_id: str,
    ) -> bool:
        """
        Trigger an Azure Automation runbook via REST API.
        Returns True on success, False on failure.
        """
        url = (
            f"{_settings.azure_automation_account_url}/jobs"
            f"?api-version=2019-06-01"
        )
        try:
            credential = DefaultAzureCredential()
            token = credential.get_token("https://management.azure.com/.default").token
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            body = {
                "properties": {
                    "runbook": {"name": runbook_name},
                    "parameters": {
                        "AlertId": alert_id,
                        **{k: str(v) for k, v in parameters.items()},
                    },
                }
            }
            resp = requests.put(url, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            logger.info("Runbook '%s' triggered for alert '%s'.", runbook_name, alert_id)
            return True
        except Exception as exc:
            logger.error("Runbook execution failed: %s", exc)
            return False

    def _persist(
        self,
        alert_id: str,
        source: str,
        log_payload: str,
        analysis: ItOpsAnalysis,
        outcome: IncidentOutcome,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        record = {
            "id": f"incident_{alert_id}",
            "alert_id": alert_id,
            "source": source,
            "log_snippet": log_payload[:500],
            "incident_summary": analysis.incident_summary,
            "root_cause": analysis.root_cause,
            "affected_components": analysis.affected_components,
            "severity": analysis.severity,
            "confidence": analysis.confidence,
            "outcome": outcome.value,
            "runbook_name": analysis.remediation.runbook_name,
            "ticket_draft": analysis.ticket_draft,
            "metadata": metadata or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._store.upsert(record)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

_ITOPS_SYSTEM_PROMPT = """
You are an expert Site Reliability Engineer (SRE) and IT operations analyst.
Analyse the provided system logs / monitoring alert and return ONLY valid JSON.

JSON Schema:
{
  "incident_summary": "<1-2 sentence plain-English summary>",
  "root_cause": "<technical root cause>",
  "affected_components": ["<component>", ...],
  "severity": "critical | high | medium | low",
  "confidence": <float 0.0-1.0 how confident you are in the diagnosis>,
  "remediation": {
    "runbook_name": "<runbook name to execute, or null if unknown>",
    "parameters": {},
    "description": "<human-readable remediation steps>"
  },
  "ticket_draft": "<full markdown ticket body for the on-call engineer>"
}

Available runbooks: restart-service, scale-out-aks, clear-disk-space,
rotate-credentials, flush-cache, failover-db, drain-node.

Set confidence >= 0.85 ONLY if you are highly certain of root cause AND
a clear runbook remediation exists.
""".strip()


def _build_itops_user_message(
    log_payload: str,
    signatures: list[str],
    metadata: dict[str, Any],
) -> str:
    sig_text = f"\n### Detected Error Signatures\n{', '.join(signatures)}" if signatures else ""
    meta_text = f"\n### Metadata\n{json.dumps(metadata, indent=2)}" if metadata else ""
    return (
        f"### Raw Log / Alert Payload\n```\n{log_payload[:8000]}\n```"
        f"{sig_text}{meta_text}"
    )
