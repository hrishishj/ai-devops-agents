"""
Unit tests for shared utilities:
  - shared/llm_client.py
  - shared/redis_cache.py
  - shared/cosmos_store.py
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

import pytest
from openai import RateLimitError, APIStatusError


# --------------------------------------------------------------------------- #
# shared/llm_client.py
# --------------------------------------------------------------------------- #

class TestAzureOpenAIClient:
    @pytest.fixture
    def client(self):
        with patch("shared.llm_client.AzureOpenAI"), \
             patch("shared.llm_client.Settings"):
            from shared.llm_client import AzureOpenAIClient
            c = AzureOpenAIClient()
            c._client = MagicMock()
            c._deployment = "gpt-4o"
            return c

    def test_validate_token_budget_raises_when_over_ceiling(self, client):
        with pytest.raises(ValueError, match="ceiling"):
            client._validate_token_budget(5000)

    def test_validate_token_budget_passes_at_ceiling(self, client):
        client._validate_token_budget(4096)  # should not raise

    def test_build_messages_includes_system_prompt(self, client):
        from shared.llm_client import LLMRequest
        req = LLMRequest(system_prompt="You are helpful.", user_message="Hello.")
        msgs = client._build_messages(req)
        assert msgs[0] == {"role": "system", "content": "You are helpful."}
        assert msgs[-1] == {"role": "user", "content": "Hello."}

    def test_build_messages_includes_few_shot(self, client):
        from shared.llm_client import LLMRequest
        req = LLMRequest(
            system_prompt="sys",
            user_message="user",
            few_shot_examples=[{"user": "example q", "assistant": "example a"}],
        )
        msgs = client._build_messages(req)
        assert any(m["role"] == "assistant" and m["content"] == "example a" for m in msgs)

    def test_backoff_increases_exponentially(self):
        from shared.llm_client import AzureOpenAIClient
        b0 = AzureOpenAIClient._backoff(0)
        b1 = AzureOpenAIClient._backoff(1)
        b2 = AzureOpenAIClient._backoff(2)
        assert b0 < b1 < b2

    def test_successful_completion_returns_response(self, client):
        from shared.llm_client import LLMRequest

        mock_usage = MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        mock_choice = MagicMock()
        mock_choice.message.content = '{"result": "ok"}'
        mock_choice.finish_reason = "stop"
        mock_raw = MagicMock(
            usage=mock_usage,
            choices=[mock_choice],
            model="gpt-4o",
        )
        client._client.chat.completions.create.return_value = mock_raw

        req = LLMRequest(system_prompt="sys", user_message="hello", max_tokens=100)
        resp = client._call_with_retry(req, client._build_messages(req), MagicMock())
        assert resp.content == '{"result": "ok"}'
        assert resp.prompt_tokens == 10

    def test_rate_limit_retried(self, client):
        from shared.llm_client import LLMRequest

        mock_usage = MagicMock(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        mock_choice = MagicMock()
        mock_choice.message.content = "ok"
        mock_choice.finish_reason = "stop"
        success = MagicMock(usage=mock_usage, choices=[mock_choice], model="gpt-4o")

        # Fail twice with RateLimitError, then succeed
        rate_err = RateLimitError.__new__(RateLimitError)
        client._client.chat.completions.create.side_effect = [
            rate_err, rate_err, success
        ]

        req = LLMRequest(system_prompt="s", user_message="u", max_tokens=100)
        with patch("time.sleep"):  # don't actually sleep in tests
            resp = client._call_with_retry(req, client._build_messages(req), MagicMock())
        assert resp.content == "ok"
        assert client._client.chat.completions.create.call_count == 3


# --------------------------------------------------------------------------- #
# shared/redis_cache.py
# --------------------------------------------------------------------------- #

class TestRedisCache:
    @pytest.fixture
    def cache(self):
        with patch("shared.redis_cache.redis.Redis"), \
             patch("shared.redis_cache.Settings"):
            from shared.redis_cache import RedisCache
            c = RedisCache()
            c._client = MagicMock()
            c._default_ttl = 3600
            return c

    def test_set_serialises_to_json(self, cache):
        cache.set("key1", {"a": 1})
        cache._client.set.assert_called_once_with("key1", json.dumps({"a": 1}), ex=3600)

    def test_get_deserialises_json(self, cache):
        cache._client.get.return_value = '{"x": 42}'
        result = cache.get("key1")
        assert result == {"x": 42}

    def test_get_missing_key_returns_none(self, cache):
        cache._client.get.return_value = None
        result = cache.get("missing")
        assert result is None

    def test_set_custom_ttl(self, cache):
        cache.set("k", "v", ttl=60)
        cache._client.set.assert_called_once_with("k", json.dumps("v"), ex=60)

    def test_exists_returns_true(self, cache):
        cache._client.exists.return_value = 1
        assert cache.exists("k") is True

    def test_exists_returns_false(self, cache):
        cache._client.exists.return_value = 0
        assert cache.exists("k") is False

    def test_delete_calls_client(self, cache):
        cache.delete("k")
        cache._client.delete.assert_called_once_with("k")

    def test_claim_idempotency_key_true_on_first_claim(self, cache):
        cache._client.set.return_value = True
        assert cache.claim_idempotency_key("idem-key") is True

    def test_claim_idempotency_key_false_on_duplicate(self, cache):
        cache._client.set.return_value = None  # NX returns None when key exists
        assert cache.claim_idempotency_key("idem-key") is False

    def test_ping_returns_true(self, cache):
        cache._client.ping.return_value = True
        assert cache.ping() is True


# --------------------------------------------------------------------------- #
# shared/cosmos_store.py
# --------------------------------------------------------------------------- #

class TestCosmosMemoryStore:
    @pytest.fixture
    def store(self):
        with patch("shared.cosmos_store.CosmosClient"), \
             patch("shared.cosmos_store.Settings"):
            from shared.cosmos_store import CosmosMemoryStore
            s = CosmosMemoryStore("test_container")
            s._container = MagicMock()
            return s

    def test_upsert_adds_updated_at(self, store):
        store._container.upsert_item.return_value = {"id": "x"}
        result = store.upsert({"id": "x"})
        call_item = store._container.upsert_item.call_args[0][0]
        assert "updated_at" in call_item

    def test_upsert_adds_default_ttl(self, store):
        store._container.upsert_item.return_value = {"id": "y"}
        store.upsert({"id": "y"})
        call_item = store._container.upsert_item.call_args[0][0]
        assert "ttl" in call_item

    def test_get_returns_item(self, store):
        store._container.read_item.return_value = {"id": "z", "data": "val"}
        item = store.get("z", "pk")
        assert item["data"] == "val"

    def test_get_missing_returns_none(self, store):
        from azure.cosmos import exceptions as cosmos_exc
        store._container.read_item.side_effect = cosmos_exc.CosmosResourceNotFoundError(
            message="Not Found", response=MagicMock(status_code=404, headers={})
        )
        result = store.get("missing", "pk")
        assert result is None

    def test_query_returns_list(self, store):
        store._container.query_items.return_value = iter([{"id": "a"}, {"id": "b"}])
        results = store.query("SELECT * FROM c")
        assert len(results) == 2

    def test_get_recent_reviews_calls_correct_query(self, store):
        store._container.query_items.return_value = iter([])
        store.get_recent_reviews("org/repo", limit=2)
        call_kwargs = store._container.query_items.call_args[1]
        params = {p["name"]: p["value"] for p in call_kwargs.get("parameters", [])}
        assert params["@repo"] == "org/repo"
        assert params["@limit"] == 2
