"""Review workflow: diff -> chunk -> discover -> ground -> rank -> publish (or shadow)."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from devopsagents.agents.review.chunking import build_chunks
from devopsagents.agents.review.grounding import Candidate, CandidateList, ground
from devopsagents.agents.review.ranking import rank
from devopsagents.core.config.repo_config import ReviewMode, parse_repo_config
from devopsagents.core.jobs.runner import NonRetryableError
from devopsagents.core.jobs.store import JobStore
from devopsagents.core.llm.gateway import LLMGateway, ModelTier
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.core.telemetry.tracing import span
from devopsagents.domain.diff import parse_unified_diff
from devopsagents.domain.jobs import Job
from devopsagents.integrations.github.client import PullRequestInfo, ReviewComment

log = logging.getLogger(__name__)

AGENT_VERSION = "review-1"
_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}


class GitHubPort(Protocol):
    def get_pr(self, inst: int, repo: str, number: int) -> PullRequestInfo: ...
    def get_diff(self, inst: int, repo: str, number: int) -> str: ...
    def get_file(self, inst: int, repo: str, path: str, ref: str) -> str | None: ...
    def post_review(
        self, inst: int, repo: str, number: int, commit_sha: str, body: str, comments: list[ReviewComment]
    ) -> int: ...


class ReviewAgent:
    def __init__(
        self,
        github: GitHubPort,
        gateway: LLMGateway,
        prompts: PromptRegistry,
        store: JobStore,
        *,
        chunk_tokens: int = 6000,
        kill_switch: bool = False,
    ) -> None:
        self._gh = github
        self._llm = gateway
        self._prompts = prompts
        self._store = store
        self._chunk_tokens = chunk_tokens
        self._kill = kill_switch

    def handle(self, job: Job) -> dict[str, Any]:
        p = job.payload
        repo, number = str(p["repo"]), int(p["number"])  # type: ignore[call-overload]
        head_sha, inst = str(p["head_sha"]), int(p.get("installation_id") or 0)  # type: ignore[call-overload]
        if self._kill:
            return {"status": "skipped", "reason": "kill switch"}

        with span("review.handle", repo=repo, pr=number, sha=head_sha):
            cfg = parse_repo_config(self._gh.get_file(inst, repo, ".ai-agents.yml", head_sha))
            if cfg.review.mode == ReviewMode.OFF:
                return {"status": "skipped", "reason": "review disabled"}

            pr = self._gh.get_pr(inst, repo, number)
            if pr.head_sha != head_sha:  # PR moved on; a newer job will handle it
                raise NonRetryableError("stale revision")
            files = parse_unified_diff(self._gh.get_diff(inst, repo, number))
            chunks, coverage = build_chunks(files, cfg.review.paths_ignore, self._chunk_tokens)

            prompt = self._prompts.get("review", "discover")
            candidates: list[Candidate] = []
            for chunk in chunks:
                if not self._store.is_current(job.id):
                    return {"status": "dropped", "reason": "superseded"}
                out = self._llm.structured(
                    tier=ModelTier.LARGE,
                    system=prompt.text,
                    user=_user_message(pr, chunk.text),
                    response_model=CandidateList,
                    scope=repo,
                    prompt_id=prompt.id,
                    max_tokens=2500,
                )
                candidates.extend(out.candidates)

            grounding = ground(candidates, files)
            to_post, overflow = rank(
                grounding.grounded,
                min_confidence=cfg.review.min_confidence,
                max_comments=cfg.review.max_comments,
            )

            result: dict[str, Any] = {
                "prompt": prompt.id,
                "agent_version": AGENT_VERSION,
                "candidates": len(candidates),
                "grounded": len(grounding.grounded),
                "dropped_ungrounded": len(grounding.dropped),
                "to_post": len(to_post),
                "overflow": len(overflow),
                "coverage": coverage.summary(),
                "comments": [c.model_dump(mode="json") for c in to_post],
            }

            if cfg.review.mode == ReviewMode.SHADOW:
                self._store.audit("review", "review.shadow", f"{repo}#{number}", {"n": len(to_post)})
                return {**result, "status": "shadow", "posted": False}

            if not self._store.is_current(job.id):  # re-check right before the side effect
                return {**result, "status": "dropped", "reason": "superseded"}

            review_id = self._gh.post_review(
                inst,
                repo,
                number,
                head_sha,
                _summary(to_post, overflow, coverage.summary()),
                [ReviewComment(c.path, c.line, _comment_body(c)) for c in to_post],
            )
            self._store.audit(
                "review",
                "review.posted",
                f"{repo}#{number}",
                {"review_id": review_id, "n": len(to_post), "sha": head_sha},
            )
            return {**result, "status": "posted", "posted": True, "review_id": review_id}


def _user_message(pr: PullRequestInfo, diff_text: str) -> str:
    return (
        f"<pr_title>{pr.title}</pr_title>\n<pr_description>{pr.body[:2000]}</pr_description>\n"
        f"<diff>\n{diff_text}\n</diff>"
    )


def _comment_body(c: Candidate) -> str:
    head = f"{_ICON[c.severity.value]} **{c.severity.value.upper()}** · _{c.category}_ — **{c.title}**"
    body = f"{head}\n\n{c.detail}"
    if c.suggestion:
        body += f"\n\n**Suggestion**\n```\n{c.suggestion}\n```"
    return body


def _summary(posted: list[Candidate], overflow: list[Candidate], coverage: str) -> str:
    lines = ["## 🤖 AI review", "", coverage]
    if posted:
        lines.append(f"\n{len(posted)} comment(s) posted, highest severity first.")
    else:
        lines.append("\nNo high-confidence issues found in the reviewed changes.")
    if overflow:
        lines.append(f"\n{len(overflow)} lower-ranked finding(s) omitted to keep this review focused.")
    lines.append("\n<sub>Automated review — verify before acting. React 👎 on a comment if it's noise.</sub>")
    return "\n".join(lines)
