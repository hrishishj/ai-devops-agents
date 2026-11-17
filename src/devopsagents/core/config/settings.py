"""Typed service settings. Built lazily — never at import time."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DA_", env_file=".env", extra="ignore")

    environment: str = "dev"
    kill_switch: bool = False  # global: stop all side effects

    # Persistence
    create_schema: bool = True  # set false in prod; run `devopsagents migrate` instead
    database_url: str = "sqlite+pysqlite:///./devopsagents.db"

    # Messaging
    servicebus_namespace: str = ""  # FQDN, e.g. x.servicebus.windows.net (managed identity)
    queue_review: str = "review"
    queue_security: str = "security"
    queue_incident: str = "incident"

    # GitHub App
    github_app_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")
    github_webhook_secret: SecretStr = SecretStr("")
    github_api_url: str = "https://api.github.com"

    # Azure OpenAI (Entra auth; no API key)
    openai_endpoint: str = ""
    openai_api_version: str = "2024-10-21"
    model_large: str = "gpt-4o"
    model_small: str = "gpt-4o-mini"

    # LLM budgets (tokens per UTC day)
    daily_token_budget_per_repo: int = 2_000_000
    daily_token_budget_global: int = 20_000_000
    max_output_tokens: int = 4096

    # Admin / alert ingress auth (shared secrets; front with Entra/APIM in prod)
    admin_token: SecretStr = SecretStr("")
    # "token": shared secret, self-declared approver (dev/pilot). "easyauth": validated Entra principal
    # injected by Container Apps Easy Auth / APIM (production; ingress must be reachable only via it).
    admin_auth: Literal["token", "easyauth"] = "token"
    approver_role: str = "Incident.Approver"
    alert_token: SecretStr = SecretStr("")

    # Incident automation
    incident_repo: str = ""  # "org/repo" where incident issues are filed
    incident_installation_id: int = 0
    log_analytics_workspace_id: str = ""
    applicationinsights_connection_string: str = ""
    automation_account_url: str = ""
    policy_file: str = "policies/default.yaml"
    runbooks_dir: str = "runbooks"
    prompts_dir: str = "prompts"

    # Review defaults
    max_review_comments: int = Field(default=15, ge=1, le=100)
    review_chunk_tokens: int = 6000

    lease_seconds: int = 300
    max_attempts: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
