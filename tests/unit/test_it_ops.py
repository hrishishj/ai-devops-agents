"""
Unit tests for agents/it_ops/agent.py
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.it_ops.agent import (
    IncidentOutcome,
    ItOpsAgent,
    ItOpsAnalysis,
    RemediationAction,
    extract_error_signatures,
    _ERROR_PATTERNS,
)


# --------------------------------------------------------------------------- #
# extract_error_signatures
# --------------------------------------------------------------------------- #

class TestExtractErrorSignatures:
    def test_detects_oom(self):
        log = "Container OOMKilled due to exceeding memory limit"
        sigs = extract_error_signatures(log)
        assert "OutOfMemory" in sigs

    def test_detects_connection_timeout(self):
        log = "Error: connection timed out after 30s"
        sigs = extract_error_signatures(log)
        assert "ConnectionTimeout" in sigs

    def test_detects_disk_full(self):
        log = "write /var/data/file: no space left on device"
        sigs = extract_error_signatures(log)
        assert "DiskFull" in sigs

    def test_detects_high_cpu(self):
        log = "CPU usage 99% sustained for 5 minutes"
        sigs = extract_error_signatures(log)
        assert "HighCPU" in sigs

    def test_detects_auth_failure(self):
        log = "Response: 401 Unauthorized from upstream service"
        sigs = extract_error_signatures(log)
        assert "AuthFailure" in sigs

    def test_detects_database_error(self):
        log = "ERROR: deadlock detected on table users"
        sigs = extract_error_signatures(log)
        assert "DatabaseError" in sigs

    def test_no_signature_clean_log(self):
        log = "INFO: Application started successfully on port 8080"
        sigs = extract_error_signatures(log)
        assert sigs == []

    def test_multiple_signatures_detected(self):
        log = "OOMKilled — connection timed out — disk full"
        sigs = extract_error_signatures(log)
        assert len(sigs) >= 2

    def test_case_insensitive(self):
        log = "OUT OF MEMORY error detected"
        sigs = extract_error_signatures(log)
        assert "OutOfMemory" in sigs


# --------------------------------------------------------------------------- #
# ItOpsAnalysis schema validation
# --------------------------------------------------------------------------- #

class TestItOpsAnalysis:
    def _valid(self, **overrides):
        base = {
            "incident_summary": "Service crashed due to OOM.",
            "root_cause": "Memory leak in worker pool.",
            "affected_components": ["worker", "cache"],
            "severity": "high",
            "confidence": 0.9,
            "remediation": {
                "runbook_name": "restart-service",
                "parameters": {"service": "worker"},
                "description": "Restart the worker service.",
            },
            "ticket_draft": "## Incident\n\nMemory exhausted.",
        }
        base.update(overrides)
        return base

    def test_valid_analysis(self):
        a = ItOpsAnalysis(**self._valid())
        assert a.severity == "high"
        assert a.confidence == 0.9

    def test_invalid_severity_raises(self):
        with pytest.raises(Exception):
            ItOpsAnalysis(**self._valid(severity="urgent"))

    def test_confidence_above_one_raises(self):
        with pytest.raises(Exception):
            ItOpsAnalysis(**self._valid(confidence=1.5))

    def test_confidence_below_zero_raises(self):
        with pytest.raises(Exception):
            ItOpsAnalysis(**self._valid(confidence=-0.1))

    def test_no_runbook_is_valid(self):
        data = self._valid()
        data["remediation"]["runbook_name"] = None
        a = ItOpsAnalysis(**data)
        assert a.remediation.runbook_name is None


# --------------------------------------------------------------------------- #
# ItOpsAgent fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_llm():
    return MagicMock()


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.upsert.side_effect = lambda item: item
    return store


@pytest.fixture
def mock_cache():
    cache = MagicMock()
    cache.claim_idempotency_key.return_value = True
    return cache


@pytest.fixture
def agent(mock_llm, mock_store, mock_cache):
    with patch("agents.it_ops.agent.get_llm_client", return_value=mock_llm), \
         patch("agents.it_ops.agent.get_cache", return_value=mock_cache), \
         patch("agents.it_ops.agent.CosmosMemoryStore", return_value=mock_store):
        a = ItOpsAgent()
        a._llm = mock_llm
        a._store = mock_store
        a._cache = mock_cache
        return a


def _make_analysis_json(confidence: float = 0.9, runbook: str = "restart-service") -> str:
    return json.dumps({
        "incident_summary": "Worker OOM crash.",
        "root_cause": "Memory leak in thread pool.",
        "affected_components": ["worker"],
        "severity": "high",
        "confidence": confidence,
        "remediation": {
            "runbook_name": runbook,
            "parameters": {},
            "description": "Restart worker service.",
        },
        "ticket_draft": "## Incident\n\nOOM crash detected.",
    })


# --------------------------------------------------------------------------- #
# handle_alert
# --------------------------------------------------------------------------- #

class TestHandleAlert:
    def test_duplicate_alert_skipped(self, agent, mock_cache):
        mock_cache.claim_idempotency_key.return_value = False
        result = agent.handle_alert("alert-001", "azure_monitor", "log data")
        assert result["outcome"] == "duplicate"

    def test_high_confidence_auto_resolves(self, agent, mock_llm):
        mock_llm.complete.return_value = MagicMock(content=_make_analysis_json(confidence=0.92))
        with patch.object(agent, "_execute_runbook", return_value=True):
            result = agent.handle_alert("alert-002", "azure_monitor", "OOMKilled")
        assert result["outcome"] == IncidentOutcome.AUTO_RESOLVED.value

    def test_low_confidence_creates_draft(self, agent, mock_llm):
        mock_llm.complete.return_value = MagicMock(content=_make_analysis_json(confidence=0.60))
        result = agent.handle_alert("alert-003", "azure_monitor", "some log")
        assert result["outcome"] == IncidentOutcome.DRAFT_CREATED.value

    def test_runbook_failure_escalates(self, agent, mock_llm):
        mock_llm.complete.return_value = MagicMock(content=_make_analysis_json(confidence=0.95))
        with patch.object(agent, "_execute_runbook", return_value=False):
            result = agent.handle_alert("alert-004", "azure_monitor", "crash log")
        assert result["outcome"] == IncidentOutcome.ESCALATED.value

    def test_llm_parse_failure_returns_error(self, agent, mock_llm):
        mock_llm.complete.return_value = MagicMock(content="not json {{{")
        result = agent.handle_alert("alert-005", "azure_monitor", "log")
        assert result["outcome"] == "error"
        assert result["reason"] == "llm_parse_failure"

    def test_no_runbook_creates_draft(self, agent, mock_llm):
        mock_llm.complete.return_value = MagicMock(
            content=_make_analysis_json(confidence=0.95, runbook=None)
        )
        result = agent.handle_alert("alert-006", "azure_monitor", "crash log")
        # High confidence but no runbook → draft
        assert result["outcome"] == IncidentOutcome.DRAFT_CREATED.value

    def test_result_includes_ticket_draft(self, agent, mock_llm):
        mock_llm.complete.return_value = MagicMock(content=_make_analysis_json(confidence=0.5))
        result = agent.handle_alert("alert-007", "azure_monitor", "log")
        assert "ticket_draft" in result
        assert len(result["ticket_draft"]) > 0

    def test_metadata_persisted(self, agent, mock_llm, mock_store):
        mock_llm.complete.return_value = MagicMock(content=_make_analysis_json(confidence=0.5))
        meta = {"subscription": "sub-123", "resource_group": "rg-prod"}
        agent.handle_alert("alert-008", "azure_monitor", "log", metadata=meta)
        call_args = mock_store.upsert.call_args[0][0]
        assert call_args["metadata"] == meta
