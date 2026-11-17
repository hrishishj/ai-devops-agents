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
    # Plain token (e.g. the Actions GITHUB_TOKEN or a PAT). Used when no GitHub App is configured.
    github_token: SecretStr = SecretStr("")
    github_api_url: str = "https://api.github.com"

    # LLM provider. Default is any OpenAI-compatible endpoint (Ollama, GitHub Models, Groq, Gemini...).
    llm_provider: Literal["openai_compat", "azure"] = "openai_compat"
    llm_base_url: str = "http://localhost:11434/v1"  # Ollama
    llm_api_key: SecretStr = SecretStr("")
    llm_model_large: str = "qwen2.5-coder:7b"
    llm_model_small: str = "qwen2.5-coder:7b"
    llm_json_mode: bool = True  # send response_format=json_object (disable if the model rejects it)

    # Azure OpenAI (optional; Entra auth, no API key)
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
    metrics_token: SecretStr = SecretStr("")  # /metrics is disabled (404) until this is set
    # "token": shared secret, self-declared approver (dev/pilot). "easyauth": validated Entra principal
    # injected by Container Apps Easy Auth / APIM (production; ingress must be reachable only via it).
    # "github": approvers send their own GitHub token; GitHub verifies identity and write access (free).
    admin_auth: Literal["token", "easyauth", "github"] = "token"
    approver_role: str = "Incident.Approver"
    approver_repo: str = ""  # github mode: approvers need push access here (defaults to incident_repo)
    approvers: str = ""  # github mode: optional comma-separated allow-list of logins
    alert_token: SecretStr = SecretStr("")

    # Verified test generation (isolated sandbox; empty = feature unavailable)
    # auto = azure_sessions if sandbox_endpoint is set, else none. "docker" = a locked-down local container.
    sandbox_kind: Literal["auto", "docker", "azure_sessions", "none"] = "auto"
    sandbox_image: str = (
        "devopsagents-sandbox"  # docker: build with `docker build -t devopsagents-sandbox infra/sandbox`
    )
    sandbox_endpoint: str = ""  # Azure Container Apps session pool management endpoint
    sandbox_session_id: str = "devopsagents"

    # Incident automation
    incident_repo: str = ""  # "org/repo" where incident issues are filed
    incident_installation_id: int = 0
    log_analytics_workspace_id: str = ""
    # Free incident path: Prometheus for evidence/verification, GitHub Actions to execute runbooks.
    prometheus_url: str = ""  # e.g. http://prometheus:9090
    prometheus_token: SecretStr = SecretStr("")
    runbook_repo: str = ""  # "org/repo" holding the runbook workflow; set to run runbooks via Actions
    runbook_workflow: str = "runbook.yml"
    runbook_ref: str = "main"
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
