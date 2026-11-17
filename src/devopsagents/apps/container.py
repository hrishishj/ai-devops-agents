"""Composition root: the only place that wires concrete adapters together."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from devopsagents.agents.incident.workflow import IncidentAgent
from devopsagents.agents.review.workflow import ReviewAgent
from devopsagents.agents.security.testgen import TestGenerator
from devopsagents.agents.security.workflow import SecurityAgent
from devopsagents.core.config.paths import resolve_data
from devopsagents.core.config.settings import Settings
from devopsagents.core.jobs.queue import InMemoryQueue, JobQueue
from devopsagents.core.jobs.runner import Handler
from devopsagents.core.jobs.store import JobStore, make_engine
from devopsagents.core.llm.gateway import LLMBackend, LLMGateway
from devopsagents.core.policy.engine import PolicyConfig, PolicyEngine
from devopsagents.core.policy.runbooks import RunbookRegistry
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.events import AlertEvent
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.azure.alert_state import AlertStateVerifier
from devopsagents.integrations.azure.automation import AutomationPort
from devopsagents.integrations.azure.evidence import Evidence, EvidenceSource, Verifier
from devopsagents.integrations.github.client import GitHubClient
from devopsagents.integrations.prometheus.client import (
    PrometheusClient,
    PrometheusEvidence,
    PrometheusVerifier,
)
from devopsagents.integrations.scanners.base import SecretsScanner, SemgrepScanner

log = logging.getLogger(__name__)


class NoBackend:
    def complete(self, **_: object) -> object:
        raise RuntimeError("LLM not configured: set DA_OPENAI_ENDPOINT")


class NullEvidence:
    def gather(self, alert: AlertEvent) -> list[Evidence]:
        return []


class NullVerifier:
    """Without a real health source, recovery is *unverifiable* -> the agent escalates."""

    def healthy(self, alert: AlertEvent) -> bool | None:
        return None


class NoAutomation:
    def start_job(self, runbook: str, params: dict[str, object]) -> str:
        raise RuntimeError("Azure Automation not configured: set DA_AUTOMATION_ACCOUNT_URL")

    def job_status(self, job_id: str) -> str:
        raise RuntimeError("Azure Automation not configured")


class LogNotifier:
    def notify(self, title: str, body: str, labels: list[str]) -> str:
        log.warning("INCIDENT TICKET (no tracker configured): %s\n%s", title, body)
        return ""


class GitHubIssueNotifier:
    def __init__(self, gh: GitHubClient, repo: str, installation_id: int) -> None:
        self._gh, self._repo, self._inst = gh, repo, installation_id

    def notify(self, title: str, body: str, labels: list[str]) -> str:
        return self._gh.create_issue(self._inst, self._repo, title, body, labels)


@dataclass
class Container:
    settings: Settings
    store: JobStore
    queue: JobQueue
    handlers: dict[AgentKind, Handler]
    github: GitHubClient | None
    registry: RunbookRegistry


def queue_for(settings: Settings, agent: AgentKind) -> str:
    return {
        AgentKind.REVIEW: settings.queue_review,
        AgentKind.SECURITY: settings.queue_security,
        AgentKind.INCIDENT: settings.queue_incident,
    }[agent]


def build_container(
    settings: Settings,
    *,
    backend: LLMBackend | None = None,
    queue: JobQueue | None = None,
    base_dir: Path | None = None,
) -> Container:
    base = base_dir or Path.cwd()
    store = JobStore(make_engine(settings.database_url), create_schema=settings.create_schema)

    if backend is None:
        if settings.llm_provider == "openai_compat":
            from devopsagents.integrations.llm.openai_compat import OpenAICompatBackend

            backend = OpenAICompatBackend(
                settings.llm_base_url,
                settings.llm_api_key.get_secret_value(),
                settings.llm_model_large,
                settings.llm_model_small,
                json_mode=settings.llm_json_mode,
            )
        elif settings.openai_endpoint:
            from devopsagents.integrations.azure.openai_backend import AzureOpenAIBackend

            backend = AzureOpenAIBackend(
                settings.openai_endpoint,
                settings.openai_api_version,
                settings.model_large,
                settings.model_small,
            )
        else:
            backend = NoBackend()  # type: ignore[assignment]  # provider=azure but no endpoint
    assert backend is not None
    gateway = LLMGateway(
        backend,
        store,
        per_scope_budget=settings.daily_token_budget_per_repo,
        global_budget=settings.daily_token_budget_global,
        max_output_tokens=settings.max_output_tokens,
        kill_switch=lambda: settings.kill_switch,
    )
    prompts = PromptRegistry(resolve_data(base, settings.prompts_dir))
    registry = RunbookRegistry.load(resolve_data(base, settings.runbooks_dir))
    policy_cfg = PolicyConfig.load(resolve_data(base, settings.policy_file))
    policy_cfg.kill_switch = policy_cfg.kill_switch or settings.kill_switch
    policy = PolicyEngine(policy_cfg, registry, store)

    if queue is None:
        if settings.servicebus_namespace:
            from devopsagents.integrations.azure.servicebus import ServiceBusQueue

            queue = ServiceBusQueue(settings.servicebus_namespace)
        else:
            queue = InMemoryQueue()  # worker also sweeps the DB, so this is safe for dev

    github: GitHubClient | None = None
    if settings.github_app_id and settings.github_app_private_key.get_secret_value():
        github = GitHubClient(
            settings.github_app_id,
            settings.github_app_private_key.get_secret_value(),
            api_url=settings.github_api_url,
        )
    elif settings.github_token.get_secret_value():  # Actions GITHUB_TOKEN / PAT, no GitHub App
        github = GitHubClient(token=settings.github_token.get_secret_value(), api_url=settings.github_api_url)

    handlers: dict[AgentKind, Handler] = {}
    if github is not None:
        handlers[AgentKind.REVIEW] = ReviewAgent(
            github,
            gateway,
            prompts,
            store,
            chunk_tokens=settings.review_chunk_tokens,
            kill_switch=settings.kill_switch,
        ).handle
        testgen: TestGenerator | None = None
        if settings.sandbox_endpoint:
            from devopsagents.integrations.sandbox.azure_sessions import AzureSessionsSandbox

            testgen = TestGenerator(
                gateway,
                prompts,
                AzureSessionsSandbox(settings.sandbox_endpoint, settings.sandbox_session_id),
            )
        handlers[AgentKind.SECURITY] = SecurityAgent(
            github,
            gateway,
            prompts,
            store,
            [SecretsScanner(), SemgrepScanner()],
            kill_switch=settings.kill_switch,
            testgen=testgen,
        ).handle

    notifier = (
        GitHubIssueNotifier(github, settings.incident_repo, settings.incident_installation_id)
        if github and settings.incident_repo
        else LogNotifier()
    )
    automation: AutomationPort = NoAutomation()
    if settings.runbook_repo and github is not None:  # free path: run runbooks as GitHub Actions workflows
        from devopsagents.integrations.github.automation import GitHubActionsAutomation

        automation = GitHubActionsAutomation(
            github, settings.runbook_repo, settings.runbook_workflow, settings.runbook_ref
        )
    elif settings.automation_account_url:
        from devopsagents.integrations.azure.automation import AutomationClient

        automation = AutomationClient(settings.automation_account_url)
    evidence: EvidenceSource = NullEvidence()
    prom: PrometheusClient | None = None
    if settings.prometheus_url:
        prom = PrometheusClient(settings.prometheus_url, token=settings.prometheus_token.get_secret_value())
    if prom is not None:
        evidence = PrometheusEvidence(prom)
    elif settings.log_analytics_workspace_id:
        from devopsagents.integrations.azure.evidence import LogAnalyticsEvidence

        evidence = LogAnalyticsEvidence(settings.log_analytics_workspace_id)
    verifier: Verifier = NullVerifier()
    if prom is not None:
        verifier = PrometheusVerifier(prom)
    elif settings.automation_account_url:
        verifier = AlertStateVerifier()
    handlers[AgentKind.INCIDENT] = IncidentAgent(
        gateway,
        prompts,
        store,
        registry,
        policy,
        evidence,
        automation,
        verifier,
        notifier,
    ).handle

    return Container(settings, store, queue, handlers, github, registry)
