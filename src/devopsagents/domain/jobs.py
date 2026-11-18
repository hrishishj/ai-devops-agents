"""Job model and idempotency-key construction."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class AgentKind(StrEnum):
    REVIEW = "review"
    SECURITY = "security"
    INCIDENT = "incident"


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # attempt failed, will be retried
    DEAD = "dead"  # attempts exhausted
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"  # newer revision exists for the same target


TERMINAL_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.DEAD, JobStatus.CANCELLED, JobStatus.SUPERSEDED}
)


def idempotency_key(agent: AgentKind, target: str, revision: str, agent_version: str) -> str:
    """`agent:target:revision:agent_version` — a new push or agent version is a new job."""
    return f"{agent.value}:{target}:{revision}:{agent_version}"


class Job(BaseModel):
    id: str
    key: str
    agent: AgentKind
    target: str  # e.g. "org/repo#42" or an incident id
    revision: str  # head_sha for PRs, alert fingerprint for incidents
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    lease_until: datetime | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    updated_at: datetime
