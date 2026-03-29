"""
Integration tests — require real Azure service connections.

Run with:
    pytest tests/integration/ -v --tb=short

Required environment variables (set via GitHub Actions secrets or .env):
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    COSMOS_ENDPOINT, COSMOS_KEY,
    REDIS_HOST, REDIS_PASSWORD
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

# Skip entire module if Azure credentials are not present
pytestmark = pytest.mark.skipif(
    not os.getenv("AZURE_OPENAI_ENDPOINT"),
    reason="Azure credentials not configured — skipping integration tests.",
)


# --------------------------------------------------------------------------- #
# Redis integration
# --------------------------------------------------------------------------- #

class TestRedisIntegration:
    @pytest.fixture
    def cache(self):
        from shared.redis_cache import RedisCache
        return RedisCache()

    def test_ping(self, cache):
        assert cache.ping() is True

    def test_set_and_get_roundtrip(self, cache):
        key = f"integration:test:{uuid.uuid4()}"
        payload = {"agent": "test", "value": 42}
        cache.set(key, payload, ttl=60)
        result = cache.get(key)
        assert result == payload
        cache.delete(key)

    def test_idempotency_key_claim(self, cache):
        key = f"idem:test:{uuid.uuid4()}"
        assert cache.claim_idempotency_key(key, ttl=30) is True
        assert cache.claim_idempotency_key(key, ttl=30) is False
        cache.delete(key)

    def test_get_missing_key_returns_none(self, cache):
        result = cache.get(f"nonexistent:{uuid.uuid4()}")
        assert result is None


# --------------------------------------------------------------------------- #
# Cosmos DB integration
# --------------------------------------------------------------------------- #

class TestCosmosIntegration:
    @pytest.fixture
    def store(self):
        from shared.cosmos_store import CosmosMemoryStore
        return CosmosMemoryStore("code_reviews")

    def test_upsert_and_get_roundtrip(self, store):
        item_id = f"integ_test_{uuid.uuid4().hex[:8]}"
        item = {
            "id": item_id,
            "repo": "org/integration-test",
            "pr_number": 9999,
            "summary": "Integration test item",
            "overall_quality": "good",
        }
        store.upsert(item)
        fetched = store.get(item_id, "org/integration-test")
        assert fetched is not None
        assert fetched["pr_number"] == 9999
        store.delete(item_id, "org/integration-test")

    def test_query_returns_results(self, store):
        item_id = f"integ_q_{uuid.uuid4().hex[:8]}"
        item = {
            "id": item_id,
            "repo": "org/query-test",
            "pr_number": 1111,
            "summary": "Query test item",
            "overall_quality": "needs_work",
        }
        store.upsert(item)
        results = store.query(
            "SELECT * FROM c WHERE c.repo = @repo",
            parameters=[{"name": "@repo", "value": "org/query-test"}],
        )
        assert any(r["id"] == item_id for r in results)
        store.delete(item_id, "org/query-test")

    def test_get_nonexistent_returns_none(self, store):
        result = store.get("definitely_does_not_exist_xyz", "org/repo")
        assert result is None


# --------------------------------------------------------------------------- #
# LLM integration
# --------------------------------------------------------------------------- #

class TestLLMIntegration:
    @pytest.fixture
    def llm(self):
        from shared.llm_client import AzureOpenAIClient
        return AzureOpenAIClient()

    def test_simple_completion(self, llm):
        from shared.llm_client import LLMRequest
        req = LLMRequest(
            system_prompt="You are a helpful assistant. Reply concisely.",
            user_message="What is 2 + 2? Reply with just the number.",
            max_tokens=10,
            temperature=0.0,
        )
        resp = llm.complete(req)
        assert "4" in resp.content
        assert resp.prompt_tokens > 0
        assert resp.completion_tokens > 0

    def test_json_response_format(self, llm):
        from shared.llm_client import LLMRequest
        import json
        req = LLMRequest(
            system_prompt="Reply ONLY with valid JSON: {\"result\": <number>}",
            user_message="What is 3 + 3?",
            max_tokens=50,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        resp = llm.complete(req)
        data = json.loads(resp.content)
        assert data["result"] == 6

    def test_token_budget_exceeded_raises(self, llm):
        from shared.llm_client import LLMRequest
        with pytest.raises(ValueError, match="ceiling"):
            req = LLMRequest(
                system_prompt="sys",
                user_message="msg",
                max_tokens=5000,   # over the 4096 ceiling
            )
            llm.complete(req)


# --------------------------------------------------------------------------- #
# IT Ops Agent integration (uses real LLM, mocked runbook execution)
# --------------------------------------------------------------------------- #

class TestItOpsAgentIntegration:
    def test_analyse_oom_alert(self):
        from agents.it_ops.agent import ItOpsAgent
        from unittest.mock import patch, MagicMock

        agent = ItOpsAgent()
        with patch("agents.it_ops.agent.get_cache") as mock_cache_fn, \
             patch("agents.it_ops.agent.CosmosMemoryStore") as mock_store_cls:
            mock_cache = MagicMock()
            mock_cache.claim_idempotency_key.return_value = True
            mock_cache_fn.return_value = mock_cache
            mock_store = MagicMock()
            mock_store.upsert.side_effect = lambda x: x
            mock_store_cls.return_value = mock_store
            agent.__post_init__()

            log = (
                "FATAL: Container OOMKilled — memory limit 512Mi exceeded.\n"
                "Pod: worker-7d9f8b-xk2p1 in namespace production.\n"
                "Last 10 log lines: processing batch of 50000 records...\n"
                "Error: killed by OOM killer."
            )
            result = agent.handle_alert(
                alert_id=f"integ-{uuid.uuid4().hex[:6]}",
                source="test",
                log_payload=log,
            )

        assert result["outcome"] in ("auto_resolved", "draft_created", "escalated")
        assert result["severity"] in ("critical", "high", "medium", "low")
        assert 0.0 <= result["confidence"] <= 1.0
        assert len(result.get("ticket_draft", "")) > 0
