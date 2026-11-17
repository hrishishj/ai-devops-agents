"""`devopsagents run-pr`: run the PR agents once, in-process (GitHub Actions mode).

No server, queue or GitHub App: it authenticates with a plain token, enqueues into an ephemeral
SQLite job store, runs each job to completion and writes a human-readable summary (shadow-mode
findings included) to the workflow step summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from devopsagents.agents.review.workflow import AGENT_VERSION as REVIEW_V
from devopsagents.agents.security.workflow import AGENT_VERSION as SECURITY_V
from devopsagents.apps.container import Container
from devopsagents.core.jobs.runner import Outcome, run_job
from devopsagents.domain.jobs import AgentKind


@dataclass
class AgentRun:
    agent: str
    outcome: str
    result: dict[str, Any] | None
    error: str | None


def run_pr(c: Container, repo: str, number: int, installation_id: int = 0) -> list[AgentRun]:
    if c.github is None:
        raise RuntimeError("no GitHub credentials: set DA_GITHUB_TOKEN (or a GitHub App)")
    pr = c.github.get_pr(installation_id, repo, number)
    payload: dict[str, object] = {
        "repo": repo,
        "number": number,
        "head_sha": pr.head_sha,
        "installation_id": installation_id,
    }
    runs: list[AgentRun] = []
    for agent, version in ((AgentKind.REVIEW, REVIEW_V), (AgentKind.SECURITY, SECURITY_V)):
        if agent not in c.handlers:
            continue
        job, _ = c.store.enqueue(
            agent, f"{repo}#{number}", pr.head_sha, version, payload, max_attempts=c.settings.max_attempts
        )
        outcome = Outcome.RETRY
        for _attempt in range(c.settings.max_attempts):
            outcome = run_job(c.store, job.id, c.handlers, c.settings.lease_seconds)
            if outcome != Outcome.RETRY:
                break
        final = c.store.get(job.id)
        runs.append(
            AgentRun(agent.value, outcome.value, c.store.result(job.id), final.error if final else None)
        )
    return runs


def render_summary(repo: str, number: int, runs: list[AgentRun]) -> str:
    out = [f"## AI agents - {repo}#{number}", ""]
    for r in runs:
        out.append(f"### {r.agent}: `{r.outcome}`")
        if r.error:
            out.append(f"\n> error: `{r.error[:300]}`")
        res = r.result or {}
        if r.agent == "review" and res:
            out.append(f"\nmode result: **{res.get('status')}** - {res.get('coverage', '')}")
            out.append(
                f"candidates {res.get('candidates')} - grounded {res.get('grounded')} - "
                f"dropped (not in diff) {res.get('dropped_ungrounded')} - to post {res.get('to_post')}"
            )
            for cm in res.get("comments", []):
                out.append(f"- `{cm['path']}:{cm['line']}` **{cm['severity']}** - {cm['title']}")
        elif r.agent == "security" and res:
            out.append(
                f"\nstatus **{res.get('status')}**, conclusion `{res.get('conclusion')}`, "
                f"{res.get('findings')} finding(s), scanners: {', '.join(res.get('scanners', []))}"
            )
        out.append("")
    return "\n".join(out)
