"""
Redis Cache client — session state, idempotency keys, and short-lived caches.

Uses SSL and password auth (Azure Cache for Redis).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis
from redis.exceptions import RedisError

from shared.config import get_settings
from shared.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

_settings = get_settings()


class RedisCache:
    """
    Thin Redis wrapper with JSON serialisation, TTL defaults,
    and OpenTelemetry spans.
    """

    def __init__(self) -> None:
        self._client = redis.Redis(
            host=_settings.redis_host,
            port=_settings.redis_port,
            password=_settings.redis_password,
            ssl=_settings.redis_ssl,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        self._default_ttl = _settings.redis_ttl_seconds

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """Serialise `value` to JSON and store with optional TTL."""
        ttl = ttl if ttl is not None else self._default_ttl
        with tracer.start_as_current_span("redis.set") as span:
            span.set_attribute("redis.key", key)
            try:
                self._client.set(key, json.dumps(value), ex=ttl)
                return True
            except RedisError as exc:
                logger.error("Redis SET failed for key '%s': %s", key, exc)
                return False

    def get(self, key: str) -> Any | None:
        """Return deserialised value, or None if absent / error."""
        with tracer.start_as_current_span("redis.get") as span:
            span.set_attribute("redis.key", key)
            try:
                raw = self._client.get(key)
                return json.loads(raw) if raw is not None else None
            except (RedisError, json.JSONDecodeError) as exc:
                logger.error("Redis GET failed for key '%s': %s", key, exc)
                return None

    def delete(self, key: str) -> bool:
        with tracer.start_as_current_span("redis.delete") as span:
            span.set_attribute("redis.key", key)
            try:
                self._client.delete(key)
                return True
            except RedisError as exc:
                logger.error("Redis DELETE failed for key '%s': %s", key, exc)
                return False

    def exists(self, key: str) -> bool:
        try:
            return bool(self._client.exists(key))
        except RedisError:
            return False

    # ------------------------------------------------------------------ #
    # Idempotency helpers
    # ------------------------------------------------------------------ #

    def claim_idempotency_key(self, key: str, ttl: int = 3600) -> bool:
        """
        Atomically set an idempotency key with NX semantics.

        Returns True if the key was claimed (first-time processing),
        False if it already exists (duplicate — skip processing).
        """
        try:
            result = self._client.set(key, "1", ex=ttl, nx=True)
            return result is True
        except RedisError as exc:
            logger.error("Redis NX failed for key '%s': %s", key, exc)
            # Fail open: allow processing to continue
            return True

    def ping(self) -> bool:
        try:
            return self._client.ping()
        except RedisError:
            return False


# Module-level singleton
_cache_singleton: RedisCache | None = None


def get_cache() -> RedisCache:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = RedisCache()
    return _cache_singleton
