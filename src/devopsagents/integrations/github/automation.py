"""Run runbooks as GitHub Actions workflows (free for public repos; 2,000 min/month for private).

The agent dispatches a `workflow_dispatch` workflow with three inputs - `runbook`, `params` (JSON)
and `correlation_id`. The workflow must set `run-name: runbook ${{ inputs.correlation_id }}` so the
run can be found again. See docs/templates/runbook.yml. Protecting the workflow's environment with
required reviewers adds a GitHub-native human approval step in front of execution.
"""

from __future__ import annotations

import json
import uuid

from devopsagents.integrations.github.client import GitHubClient


class GitHubActionsAutomation:
    def __init__(
        self,
        client: GitHubClient,
        repo: str,
        workflow: str = "runbook.yml",
        ref: str = "main",
        installation_id: int = 0,
    ) -> None:
        self._gh, self._repo, self._wf, self._ref, self._inst = client, repo, workflow, ref, installation_id

    def start_job(self, runbook: str, params: dict[str, object]) -> str:
        correlation_id = uuid.uuid4().hex
        self._gh.dispatch_workflow(
            self._inst,
            self._repo,
            self._wf,
            self._ref,
            {
                "runbook": runbook,
                "params": json.dumps(params, sort_keys=True, default=str),
                "correlation_id": correlation_id,
            },
        )
        return correlation_id

    def job_status(self, job_id: str) -> str:
        """Maps a workflow run onto Azure Automation's vocabulary (Running/Completed/Failed)."""
        for run in self._gh.list_workflow_runs(self._inst, self._repo, self._wf):
            title = f"{run.get('name') or ''} {run.get('display_title') or ''}"
            if job_id not in title:
                continue
            if run.get("status") != "completed":
                return "Running"  # queued, in progress, or waiting for environment approval
            return "Completed" if run.get("conclusion") == "success" else "Failed"
        return "Running"  # the dispatch has not produced a run yet
