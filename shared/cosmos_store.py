"""
Azure Cosmos DB (NoSQL) memory store.

Used by agents to persist and retrieve:
  - Historical code reviews  (container: code_reviews)
  - IT incident records      (container: it_incidents)
  - Test/security reports    (container: test_reports)

All items follow a common envelope schema with a TTL field for automatic expiry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from azure.cosmos import CosmosClient, ContainerProxy, exceptions as cosmos_exc
from azure.identity import DefaultAzureCredential

from shared.config import get_settings
from shared.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

_settings = get_settings()

# TTL in seconds — 90 days
_DEFAULT_TTL = 60 * 60 * 24 * 90


class CosmosMemoryStore:
    """
    Generic Cosmos DB CRUD wrapper used as an agent memory layer.

    Parameters
    ----------
    container_name : str
        Cosmos container to operate on.
    """

    def __init__(self, container_name: str) -> None:
        self._container_name = container_name
        self._client = self._build_client()
        self._container: ContainerProxy = (
            self._client
            .get_database_client(_settings.cosmos_database)
            .get_container_client(container_name)
        )

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def upsert(self, item: dict[str, Any]) -> dict[str, Any]:
        """Insert or replace an item. Adds `updated_at` and `ttl` fields."""
        item.setdefault("ttl", _DEFAULT_TTL)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()

        with tracer.start_as_current_span("cosmos.upsert") as span:
            span.set_attribute("cosmos.container", self._container_name)
            span.set_attribute("cosmos.item_id", item.get("id", ""))
            result = self._container.upsert_item(item)
        return result

    def get(self, item_id: str, partition_key: str) -> dict[str, Any] | None:
        """Return item by id + partition key, or None if not found."""
        with tracer.start_as_current_span("cosmos.get") as span:
            span.set_attribute("cosmos.container", self._container_name)
            span.set_attribute("cosmos.item_id", item_id)
            try:
                return self._container.read_item(item_id, partition_key=partition_key)
            except cosmos_exc.CosmosResourceNotFoundError:
                return None

    def query(
        self,
        query: str,
        parameters: list[dict[str, Any]] | None = None,
        max_items: int = 20,
    ) -> list[dict[str, Any]]:
        """Run a parameterised SQL query and return up to `max_items` results."""
        with tracer.start_as_current_span("cosmos.query") as span:
            span.set_attribute("cosmos.container", self._container_name)
            items = list(
                self._container.query_items(
                    query=query,
                    parameters=parameters or [],
                    enable_cross_partition_query=True,
                    max_item_count=max_items,
                )
            )
        return items

    def delete(self, item_id: str, partition_key: str) -> None:
        """Delete item by id + partition key (no-op if not found)."""
        with tracer.start_as_current_span("cosmos.delete") as span:
            span.set_attribute("cosmos.container", self._container_name)
            try:
                self._container.delete_item(item_id, partition_key=partition_key)
            except cosmos_exc.CosmosResourceNotFoundError:
                logger.debug("Item %s not found for deletion — skipping.", item_id)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def get_recent_reviews(self, repo: str, limit: int = 3) -> list[dict[str, Any]]:
        """
        Retrieve the `limit` most recent code reviews for a given repo.
        Used by the Coding Co-Pilot context builder.
        """
        query = (
            "SELECT TOP @limit * FROM c "
            "WHERE c.repo = @repo "
            "ORDER BY c.updated_at DESC"
        )
        return self.query(
            query,
            parameters=[
                {"name": "@repo", "value": repo},
                {"name": "@limit", "value": limit},
            ],
            max_items=limit,
        )

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_client() -> CosmosClient:
        if _settings.cosmos_key:
            return CosmosClient(
                url=_settings.cosmos_endpoint,
                credential=_settings.cosmos_key,
            )
        # Managed Identity in production
        credential = DefaultAzureCredential()
        return CosmosClient(url=_settings.cosmos_endpoint, credential=credential)
