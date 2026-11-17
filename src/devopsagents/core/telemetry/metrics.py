"""Prometheus text-format rendering (no client library needed).

Everything is derived from the database (see JobStore.metrics_snapshot), so it works the same
whether the API and workers run in one process or many.
"""

from __future__ import annotations

from typing import Any

from devopsagents.domain.jobs import AgentKind, JobStatus

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: str) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}" if labels else ""


def render_prometheus(snapshot: dict[str, Any], *, kill_switch: bool, version: str) -> str:
    out: list[str] = []

    def metric(name: str, help_: str, kind: str, samples: list[tuple[str, float]]) -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{lab} {value:g}" for lab, value in samples)

    metric("devopsagents_build_info", "Build information.", "gauge", [(_labels(version=version), 1)])
    metric(
        "devopsagents_kill_switch",
        "1 when the global kill switch is on.",
        "gauge",
        [("", 1 if kill_switch else 0)],
    )

    jobs = snapshot["jobs"]
    metric(
        "devopsagents_jobs",
        "Jobs by agent and status (zero-filled so alerts never see a missing series).",
        "gauge",
        [
            (_labels(agent=a.value, status=s.value), jobs.get((a.value, s.value), 0))
            for a in AgentKind
            for s in JobStatus
        ],
    )
    oldest = snapshot["oldest_queued_age"]
    metric(
        "devopsagents_oldest_queued_job_age_seconds",
        "Age of the oldest job still waiting to run.",
        "gauge",
        [(_labels(agent=a.value), oldest.get(a.value, 0.0)) for a in AgentKind],
    )
    metric(
        "devopsagents_audit_events_total",
        "Audit events by type (side effects the agents performed).",
        "counter",
        [(_labels(event=e), n) for e, n in sorted(snapshot["audit"].items())],
    )
    metric(
        "devopsagents_llm_tokens_today",
        "LLM tokens used today (UTC); top scopes plus global.",
        "gauge",
        [(_labels(scope=sc), n) for sc, n in snapshot["tokens_today"].items()],
    )
    return "\n".join(out) + "\n"
