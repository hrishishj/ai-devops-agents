"""
Unit tests for api/main.py — webhook routes, HMAC verification, health probes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    with patch("shared.telemetry.init_telemetry"), \
         patch("shared.config.Settings"), \
         patch("shared.redis_cache.redis.Redis"), \
         patch("shared.cosmos_store.CosmosClient"), \
         patch("shared.llm_client.AzureOpenAI"):
        from api.main import app
        return TestClient(app)


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Health probes
# --------------------------------------------------------------------------- #

class TestHealthProbes:
    def test_liveness_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readiness_with_healthy_redis(self, client):
        with patch("api.main.get_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.ping.return_value = True
            mock_cache_fn.return_value = mock_cache
            resp = client.get("/health/ready")
        assert resp.status_code == 200

    def test_readiness_with_dead_redis(self, client):
        with patch("api.main.get_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.ping.return_value = False
            mock_cache_fn.return_value = mock_cache
            resp = client.get("/health/ready")
        assert resp.status_code == 503


# --------------------------------------------------------------------------- #
# HMAC verification
# --------------------------------------------------------------------------- #

class TestHmacVerification:
    def test_valid_signature_accepted(self):
        from api.main import _verify_github_signature
        secret = "my-webhook-secret"
        body = b'{"action": "opened"}'
        sig = _sign(body, secret)
        with patch("api.main._settings") as ms:
            ms.github_webhook_secret = secret
            assert _verify_github_signature(body, sig) is True

    def test_invalid_signature_rejected(self):
        from api.main import _verify_github_signature
        with patch("api.main._settings") as ms:
            ms.github_webhook_secret = "secret"
            assert _verify_github_signature(b"body", "sha256=invalid") is False

    def test_missing_signature_rejected(self):
        from api.main import _verify_github_signature
        with patch("api.main._settings") as ms:
            ms.github_webhook_secret = "secret"
            assert _verify_github_signature(b"body", None) is False

    def test_empty_secret_bypasses_check(self):
        from api.main import _verify_github_signature
        with patch("api.main._settings") as ms:
            ms.github_webhook_secret = ""
            # Should return True (no secret configured — dev mode)
            assert _verify_github_signature(b"body", "sha256=anything") is True


# --------------------------------------------------------------------------- #
# GitHub Webhook route
# --------------------------------------------------------------------------- #

class TestGitHubWebhook:
    def _pr_payload(self, action: str = "opened") -> dict:
        return {
            "action": action,
            "pull_request": {"number": 99},
            "repository": {"full_name": "org/repo"},
        }

    def test_pr_opened_accepted(self, client):
        payload = json.dumps(self._pr_payload("opened")).encode()
        with patch("api.main._verify_github_signature", return_value=True), \
             patch("api.main._run_coding_review"), \
             patch("api.main._run_security_scan"):
            resp = client.post(
                "/webhooks/github",
                content=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": "sha256=mock",
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 202
        assert resp.json()["event"] == "pull_request"

    def test_pr_closed_not_dispatched(self, client):
        payload = json.dumps(self._pr_payload("closed")).encode()
        with patch("api.main._verify_github_signature", return_value=True), \
             patch("api.main._run_coding_review") as mock_review:
            resp = client.post(
                "/webhooks/github",
                content=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": "sha256=mock",
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 202
        mock_review.assert_not_called()

    def test_invalid_signature_returns_403(self, client):
        payload = b'{"action": "opened"}'
        with patch("api.main._settings") as ms:
            ms.github_webhook_secret = "real-secret"
            resp = client.post(
                "/webhooks/github",
                content=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": "sha256=wrong",
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 403

    def test_invalid_json_returns_400(self, client):
        with patch("api.main._verify_github_signature", return_value=True):
            resp = client.post(
                "/webhooks/github",
                content=b"not-json",
                headers={
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": "sha256=mock",
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Azure Monitor Webhook
# --------------------------------------------------------------------------- #

class TestAzureMonitorWebhook:
    def test_valid_alert_accepted(self, client):
        payload = json.dumps({
            "data": {
                "essentials": {
                    "alertId": "/subscriptions/xxx/alerts/alert-001",
                    "description": "High CPU",
                    "subscriptionId": "sub-xxx",
                    "resourceGroupName": "rg-prod",
                    "alertRule": "HighCPUAlert",
                }
            }
        })
        with patch("api.main._run_itops_analysis"):
            resp = client.post(
                "/webhooks/azure-monitor",
                content=payload.encode(),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 202

    def test_invalid_json_returns_400(self, client):
        resp = client.post(
            "/webhooks/azure-monitor",
            content=b"bad json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Direct API triggers
# --------------------------------------------------------------------------- #

class TestDirectApiTriggers:
    def test_direct_review_trigger(self, client):
        with patch("api.main._run_coding_review"):
            resp = client.post(
                "/api/v1/review",
                json={"repo_full_name": "org/repo", "pr_number": 42},
            )
        assert resp.status_code == 202
        assert resp.json()["pr_number"] == 42

    def test_direct_itops_trigger(self, client):
        with patch("api.main._run_itops_analysis"):
            resp = client.post(
                "/api/v1/it-ops/analyse",
                json={
                    "alert_id": "alert-123",
                    "source": "manual",
                    "log_payload": "OOMKilled",
                },
            )
        assert resp.status_code == 202
        assert resp.json()["alert_id"] == "alert-123"
