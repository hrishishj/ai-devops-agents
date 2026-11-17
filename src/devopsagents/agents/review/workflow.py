"""Review workflow: diff -> chunk -> discover -> ground -> rank -> publish (or shadow)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from devopsagents.agents.review.chunking import build_chunks
from devopsagents.agents.review.grounding import Candidate, CandidateList, ground
from devopsagents.agents.review.ranking import candidate_fp, rank
from devopsagents.core.config.repo_config import ReviewMode, parse_repo_config
from devopsagents.core.jobs.runner import NonRetryableError
from devopsagents.core.jobs.store import JobStore
from devopsagents.core.llm.gateway import LLMGateway, ModelTier
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.core.telemetry.tracing import span
from devopsagents.domain.diff import parse_unified_diff
from devopsagents.domain.jobs import Job
from devopsagents.integrations.github.client import PullRequestInfo, ReviewComment, ReviewThread

log = logging.getLogger(__name__)

AGENT_VERSION = "review-1"
# Hidden marker on every comment we post, so later runs can recognise (and learn from) our own threads.
_MARKER_RE = re.compile(r"<!-- da:fp=([0-9a-f]{16}) -->")
_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}


class GitHubPort(Protocol):
    def get_pr(self, inst: int, repo: str, number: int) -> PullRequestInfo: ...
    def get_diff(self, inst: int, repo: str, number: int) -> str: ...
    def get_file(self, inst: int, repo: str, path: str, ref: str) -> str | None: ...
    def post_review(
        self, inst: int, repo: str, number: int, commit_sha: str, body: str, comments: list[ReviewComment]
    ) -> int: ...
    def review_threads(self, inst: int, repo: str, number: int) -> tuple[str, list[ReviewThread]]: ...
    def resolve_review_thread(self, inst: int, thread_id: str) -> None: ...


@dataclass
class _Feedback:
    existing: set[str] = field(default_factory=set)  # our still-open threads: do not duplicate
    dismissed: set[str] = field(default_factory=set)  # a human resolved it or reacted with a thumbs-down
    outdated: list[str] = field(default_factory=list)  # our open threads whose code has since changed


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
            feedback = self._read_feedback(inst, repo, number)
            for fp in feedback.dismissed:
                self._store.record_feedback(repo, fp)  # remembered across pull requests
            suppressed = feedback.dismissed | feedback.existing | self._store.dismissed_fingerprints(repo)
            by_feedback = sum(1 for c in grounding.grounded if candidate_fp(c) in suppressed)
            to_post, overflow = rank(
                grounding.grounded,
                min_confidence=cfg.review.min_confidence,
                max_comments=cfg.review.max_comments,
                suppressed=suppressed,
            )

            result: dict[str, Any] = {
                "prompt": prompt.id,
                "agent_version": AGENT_VERSION,
                "candidates": len(candidates),
                "grounded": len(grounding.grounded),
                "dropped_ungrounded": len(grounding.dropped),
                "to_post": len(to_post),
                "overflow": len(overflow),
                "suppressed_by_feedback": by_feedback,
                "coverage": coverage.summary(),
                "comments": [c.model_dump(mode="json") for c in to_post],
            }

            if cfg.review.mode == ReviewMode.SHADOW:
                self._store.audit("review", "review.shadow", f"{repo}#{number}", {"n": len(to_post)})
                return {
                    **result,
                    "status": "shadow",
                    "posted": False,
                    "would_resolve": len(feedback.outdated),  # shadow mode changes nothing on the PR
                }

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
            resolved = self._resolve_outdated(inst, feedback.outdated, f"{repo}#{number}")
            return {
                **result,
                "status": "posted",
                "posted": True,
                "review_id": review_id,
                "threads_resolved": resolved,
            }

    def _read_feedback(self, inst: int, repo: str, number: int) -> _Feedback:
        """What reviewers did with our earlier comments on this PR. Best effort: a failure here (for
        example a token without GraphQL access) must never stop the review itself."""
        fb = _Feedback()
        try:
            viewer, threads = self._gh.review_threads(inst, repo, number)
        except Exception:
            log.warning("could not read review threads for %s#%d; continuing without feedback", repo, number)
            return fb
        for t in threads:
            m = _MARKER_RE.search(t.body)
            if m is None:
                continue  # not ours
            fp = m.group(1)
            resolved_by_human = t.is_resolved and (t.resolved_by or "") != viewer
            if resolved_by_human or t.thumbs_down > 0:
                fb.dismissed.add(fp)
            elif not t.is_resolved and t.is_outdated:
                fb.outdated.append(t.id)  # the code changed; the finding may now be elsewhere or fixed
            elif not t.is_resolved:
                fb.existing.add(fp)
        return fb

    def _resolve_outdated(self, inst: int, thread_ids: list[str], subject: str) -> int:
        done = 0
        for tid in thread_ids:
            try:
                self._gh.resolve_review_thread(inst, tid)
                done += 1
            except Exception:
                log.warning("could not resolve thread %s on %s", tid, subject)
        if done:
            self._store.audit("review", "review.threads_resolved", subject, {"n": done})
        return done


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
    # Hidden marker: lets later runs recognise our own threads and learn from how reviewers treated them.
    return body + f"\n\n<!-- da:fp={candidate_fp(c)} -->"


def _summary(posted: list[Candidate], overflow: list[Candidate], coverage: str) -> str:
    lines = ["## 🤖 AI review", "", coverage]
    if posted:
        lines.append(f"\n{len(posted)} comment(s) posted, highest severity first.")
    else:
        lines.append("\nNo high-confidence issues found in the reviewed changes.")
    if overflow:
        lines.append(f"\n{len(overflow)} lower-ranked finding(s) omitted to keep this review focused.")
    lines.append(
        "\n<sub>Automated review — verify before acting. "
        "React 👎 or resolve a thread to stop similar comments.</sub>"
    )
    return "\n".join(lines)
