"""
Application configuration — all secrets resolved from Azure Key Vault
at startup via Managed Identity. Falls back to environment variables
for local development (populated by .env or the dev container).
"""

from __future__ import annotations

import logging
from functools import cached_property

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """
    Central configuration object.

    Precedence (highest → lowest):
        1. Azure Key Vault  (production)
        2. Environment variables / .env file  (local / CI)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Azure Core
    # ------------------------------------------------------------------ #
    azure_keyvault_url: str = Field(
        default="",
        description="https://<vault-name>.vault.azure.net/",
    )
    azure_subscription_id: str = Field(default="")

    # ------------------------------------------------------------------ #
    # Azure OpenAI
    # ------------------------------------------------------------------ #
    azure_openai_endpoint: str = Field(default="")
    azure_openai_api_key: str = Field(default="")
    azure_openai_api_version: str = Field(default="2024-02-01")
    azure_openai_deployment: str = Field(default="gpt-4o")

    # ------------------------------------------------------------------ #
    # Cosmos DB
    # ------------------------------------------------------------------ #
    cosmos_endpoint: str = Field(default="")
    cosmos_key: str = Field(default="")
    cosmos_database: str = Field(default="ai_agents_db")
    cosmos_container_reviews: str = Field(default="code_reviews")
    cosmos_container_incidents: str = Field(default="it_incidents")

    # ------------------------------------------------------------------ #
    # Redis Cache
    # ------------------------------------------------------------------ #
    redis_host: str = Field(default="localhost")
    redis_port: int = Field(default=6380)
    redis_password: str = Field(default="")
    redis_ssl: bool = Field(default=True)
    redis_ttl_seconds: int = Field(default=3600)

    # ------------------------------------------------------------------ #
    # Azure Service Bus
    # ------------------------------------------------------------------ #
    servicebus_connection_string: str = Field(default="")
    servicebus_queue_coding: str = Field(default="coding-copilot-queue")
    servicebus_queue_itops: str = Field(default="it-ops-queue")
    servicebus_queue_security: str = Field(default="test-security-queue")

    # ------------------------------------------------------------------ #
    # GitHub
    # ------------------------------------------------------------------ #
    github_app_id: str = Field(default="")
    github_app_private_key: str = Field(default="")
    github_webhook_secret: str = Field(default="")
    github_token: str = Field(default="")  # PAT for local dev / CI

    # ------------------------------------------------------------------ #
    # Azure Monitor / App Insights
    # ------------------------------------------------------------------ #
    applicationinsights_connection_string: str = Field(default="")
    otel_service_name: str = Field(default="ai-devops-agents")

    # ------------------------------------------------------------------ #
    # IT Ops Agent
    # ------------------------------------------------------------------ #
    azure_automation_account_url: str = Field(default="")
    confidence_threshold_auto_resolve: float = Field(default=0.85)

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("redis_port")
    @classmethod
    def valid_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError(f"Invalid port: {v}")
        return v

    @field_validator("confidence_threshold_auto_resolve")
    @classmethod
    def valid_threshold(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("Confidence threshold must be between 0 and 1.")
        return v

    # ------------------------------------------------------------------ #
    # Key Vault bootstrap
    # ------------------------------------------------------------------ #
    @cached_property
    def _kv_client(self) -> SecretClient | None:
        if not self.azure_keyvault_url:
            return None
        try:
            credential = DefaultAzureCredential()
            return SecretClient(vault_url=self.azure_keyvault_url, credential=credential)
        except Exception as exc:
            logger.warning("Key Vault client init failed: %s", exc)
            return None

    def get_secret(self, name: str, default: str = "") -> str:
        """
        Resolve a secret from Key Vault; fall back to the environment
        variable of the same name (lower-cased, hyphens → underscores).
        """
        client = self._kv_client
        if client is not None:
            try:
                return client.get_secret(name).value or default
            except Exception as exc:
                logger.debug("Secret '%s' not in Key Vault: %s", name, exc)
        env_name = name.replace("-", "_").lower()
        return getattr(self, env_name, default) or default


# ------------------------------------------------------------------ #
# Module-level singleton
# ------------------------------------------------------------------ #
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
