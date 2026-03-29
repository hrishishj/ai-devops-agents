"""
Coding Co-Pilot Agent
=====================
Performs AI-assisted code review on GitHub Pull Requests.

Pipeline
--------
  1. Context Builder  — fetches diff, file tree, and up to 3 historical reviews
  2. Prompt Engine    — assembles structured prompt with few-shot examples
  3. LLM Call         — GPT-4o returns structured JSON
  4. Response Parser  — validates JSON schema
  5. GitHub Action    — posts inline review comments on the PR
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError, field_validator

from shared.config import get_settings
from shared.cosmos_store import CosmosMemoryStore
from shared.github_client import (
    InlineComment,
    PullRequestContext,
    fetch_pr_context,
    post_review_comments,
)
from shared.llm_client import LLMRequest, get_llm_client
from shared.redis_cache import get_cache
from shared.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)
_settings = get_settings()


# --------------------------------------------------------------------------- #
# Response schema (Pydantic validation of GPT-4o JSON output)
# --------------------------------------------------------------------------- #

class ReviewComment(BaseModel):
    path: str
    line: int
    severity: str            # "critical" | "warning" | "suggestion" | "info"
    category: str            # "bug" | "security" | "perf" | "style" | "docs"
    body: str                # human-readable markdown comment
    suggestion: str | None = None  # optional code snippet suggestion

    @field_validator("severity")
    @classmethod
    def valid_severity(cls, v: str) -> str:
        allowed = {"critical", "warning", "suggestion", "info"}
        if v not in allowed:
            raise ValueError(f"severity must be one of {allowed}")
        return v

    @field_validator("line")
    @classmethod
    def positive_line(cls, v: int) -> int:
        if v < 1:
            raise ValueError("line must be >= 1")
        return v


class ReviewResponse(BaseModel):
    summary: str
    overall_quality: str     # "good" | "needs_work" | "major_issues"
    comments: list[ReviewComment]

    @field_validator("overall_quality")
    @classmethod
    def valid_quality(cls, v: str) -> str:
        allowed = {"good", "needs_work", "major_issues"}
        if v not in allowed:
            raise ValueError(f"overall_quality must be one of {allowed}")
        return v


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #

@dataclass
class CodingCopilotAgent:
    """
    Orchestrates the full code-review pipeline for a single PR.
    """

    _llm = None
    _store: CosmosMemoryStore = field(
        default_factory=lambda: CosmosMemoryStore(get_settings().cosmos_container_reviews)
    )
    _cache = None

    def __post_init__(self) -> None:
        self._llm = get_llm_client()
        self._cache = get_cache()

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def handle_pr(self, repo_full_name: str, pr_number: int) -> None:
        """
        Full pipeline entry point — called by the webhook handler or
        GitHub Actions step.
        """
        idempotency_key = f"coding_copilot:{repo_full_name}:{pr_number}"

        if not self._cache.claim_idempotency_key(idempotency_key):
            logger.info("Duplicate PR event — skipping: %s#%d", repo_full_name, pr_number)
            return

        with tracer.start_as_current_span("coding_copilot.handle_pr") as span:
            span.set_attribute("github.repo", repo_full_name)
            span.set_attribute("github.pr_number", pr_number)

            # 1. Fetch PR context
            pr_ctx = fetch_pr_context(repo_full_name, pr_number)

            # 2. Build enriched context
            enriched = self._build_context(pr_ctx)

            # 3. Call LLM
            raw_response = self._call_llm(enriched, pr_ctx)

            # 4. Parse & validate
            review = self._parse_response(raw_response)
            if review is None:
                logger.error("Failed to parse LLM response for PR #%d. Aborting.", pr_number)
                return

            # 5. Post comments to GitHub
            self._post_comments(pr_ctx, review)

            # 6. Persist to Cosmos DB
            self._persist(repo_full_name, pr_number, pr_ctx, review)

            span.set_attribute("review.comment_count", len(review.comments))
            span.set_attribute("review.overall_quality", review.overall_quality)

    # ------------------------------------------------------------------ #
    # Pipeline stages
    # ------------------------------------------------------------------ #

    def _build_context(self, pr_ctx: PullRequestContext) -> dict:
        """Fetch historical reviews and assemble context dict."""
        history = self._store.get_recent_reviews(pr_ctx.repo_full_name, limit=3)
        return {
            "pr_title": pr_ctx.title,
            "pr_body": pr_ctx.body,
            "changed_files": pr_ctx.changed_files,
            "diff": pr_ctx.diff[:12_000],   # trim to stay within context window
            "historical_reviews": [
                {
                    "pr_number": h.get("pr_number"),
                    "summary": h.get("summary"),
                    "overall_quality": h.get("overall_quality"),
                }
                for h in history
            ],
        }

    def _call_llm(self, context: dict, pr_ctx: PullRequestContext) -> str:
        request = LLMRequest(
            system_prompt=_SYSTEM_PROMPT,
            user_message=_build_user_message(context),
            temperature=0.1,
            max_tokens=3000,
            response_format={"type": "json_object"},
            few_shot_examples=_FEW_SHOT_EXAMPLES,
        )
        response = self._llm.complete(request)
        return response.content

    def _parse_response(self, raw: str) -> ReviewResponse | None:
        try:
            data = json.loads(raw)
            return ReviewResponse(**data)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.error("Review response parsing failed: %s", exc)
            logger.debug("Raw LLM output: %s", raw)
            return None

    def _post_comments(self, pr_ctx: PullRequestContext, review: ReviewResponse) -> None:
        severity_emoji = {
            "critical": "🔴",
            "warning": "🟡",
            "suggestion": "🔵",
            "info": "ℹ️",
        }
        inline_comments = [
            InlineComment(
                path=c.path,
                line=c.line,
                body=(
                    f"{severity_emoji.get(c.severity, '')} **[{c.severity.upper()}]** "
                    f"_{c.category}_\n\n{c.body}"
                    + (f"\n\n**Suggested fix:**\n```\n{c.suggestion}\n```" if c.suggestion else "")
                ),
            )
            for c in review.comments
        ]

        summary = (
            f"## 🤖 AI Code Review\n\n"
            f"**Overall Quality:** `{review.overall_quality}`\n\n"
            f"{review.summary}\n\n"
            f"Found **{len(review.comments)}** comment(s)."
        )

        post_review_comments(
            repo_full_name=pr_ctx.repo_full_name,
            pr_number=pr_ctx.pr_number,
            commit_sha=pr_ctx.head_sha,
            comments=inline_comments,
            summary_body=summary,
        )

    def _persist(
        self,
        repo: str,
        pr_number: int,
        pr_ctx: PullRequestContext,
        review: ReviewResponse,
    ) -> None:
        item = {
            "id": f"{repo.replace('/', '_')}_{pr_number}",
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": pr_ctx.head_sha,
            "summary": review.summary,
            "overall_quality": review.overall_quality,
            "comment_count": len(review.comments),
            "changed_files": pr_ctx.changed_files,
        }
        self._store.upsert(item)


# --------------------------------------------------------------------------- #
# Prompts & Few-Shot Examples
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = """
You are an expert code reviewer. Your job is to analyse a GitHub Pull Request diff and produce
a structured JSON review.

Rules:
- Be concise, technical, and constructive.
- Only comment on the changed lines in the diff (do not invent issues in unchanged code).
- Assign correct severity: critical (bugs/security), warning (code-smell/perf), suggestion (style/best-practice), info (FYI).
- Return ONLY valid JSON matching the schema below — no preamble, no markdown fences.

JSON Schema:
{
  "summary": "<1-3 sentence overall summary>",
  "overall_quality": "good | needs_work | major_issues",
  "comments": [
    {
      "path": "<relative file path>",
      "line": <line number in new file>,
      "severity": "critical | warning | suggestion | info",
      "category": "bug | security | perf | style | docs",
      "body": "<markdown explanation>",
      "suggestion": "<optional code snippet>"
    }
  ]
}
""".strip()


def _build_user_message(context: dict) -> str:
    history_text = ""
    if context["historical_reviews"]:
        history_text = "\n\n### Historical Review Context\n" + json.dumps(
            context["historical_reviews"], indent=2
        )

    return (
        f"### PR Title\n{context['pr_title']}\n\n"
        f"### PR Description\n{context['pr_body'] or '(no description)'}\n\n"
        f"### Changed Files\n{', '.join(context['changed_files'])}\n"
        f"{history_text}\n\n"
        f"### Diff\n```diff\n{context['diff']}\n```"
    )


_FEW_SHOT_EXAMPLES = [
    {
        "user": (
            "### PR Title\nFix auth token refresh\n\n"
            "### Diff\n```diff\n+    token = request.headers['Authorization']\n"
            "+    db.execute(f'SELECT * FROM users WHERE token={token}')\n```"
        ),
        "assistant": json.dumps(
            {
                "summary": "The PR introduces a SQL injection vulnerability by embedding user input directly in a query.",
                "overall_quality": "major_issues",
                "comments": [
                    {
                        "path": "auth/handler.py",
                        "line": 2,
                        "severity": "critical",
                        "category": "security",
                        "body": "Direct string interpolation in SQL query creates a SQL injection vulnerability.",
                        "suggestion": "db.execute('SELECT * FROM users WHERE token = ?', (token,))",
                    }
                ],
            }
        ),
    }
]
