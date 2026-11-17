"""Normalised inbound events (source-agnostic)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PullRequestEvent(BaseModel):
    delivery_id: str
    repo: str  # "org/repo"
    number: int
    head_sha: str
    base_sha: str = ""
    installation_id: int | None = None
    action: str = "opened"

    @property
    def target(self) -> str:
        return f"{self.repo}#{self.number}"


class AlertEvent(BaseModel):
    alert_id: str
    source: str
    title: str
    severity: str = "unknown"  # sev0..sev4 / critical.. normalised by caller
    resource_id: str = ""
    environment: str = "prod"
    fired_at: str = ""
    raw: dict[str, object] = Field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Alerts for the same rule+resource correlate to one incident."""
        return f"{self.resource_id}|{self.title}".lower()
