"""
GitHub SDK helpers — PR diff fetching, inline comment posting,
commit creation, and App authentication.

Auth strategy
-------------
  * GitHub App (production): uses RSA private key + App ID from Key Vault
    to generate a short-lived installation token.
  * PAT (local / CI): falls back to GITHUB_TOKEN env variable.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

import jwt
import requests
from github import Github, GithubIntegration, Auth
from github.PullRequest import PullRequest
from github.Repository import Repository

from shared.config import get_settings
from shared.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

_settings = get_settings()


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #

@dataclass
class InlineComment:
    path: str           # relative file path in the repo
    line: int           # line number in the new file
    body: str           # markdown body of the comment
    side: str = "RIGHT" # "LEFT" or "RIGHT"


@dataclass
class PullRequestContext:
    repo_full_name: str   # "org/repo"
    pr_number: int
    base_sha: str
    head_sha: str
    diff: str
    changed_files: list[str]
    title: str
    body: str


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def _get_github_client() -> Github:
    """Return an authenticated GitHub client."""
    if _settings.github_app_id and _settings.github_app_private_key:
        private_key = _settings.github_app_private_key.replace("\\n", "\n")
        auth = Auth.AppAuth(
            app_id=int(_settings.github_app_id),
            private_key=private_key,
        )
        gi = GithubIntegration(auth=auth)
        # Get the first installation token
        installations = list(gi.get_installations())
        if installations:
            token = installations[0].get_github_for_installation().get_user().login
            return installations[0].get_github_for_installation()
    # PAT fallback
    return Github(auth=Auth.Token(_settings.github_token))


# --------------------------------------------------------------------------- #
# PR helpers
# --------------------------------------------------------------------------- #

def fetch_pr_context(repo_full_name: str, pr_number: int) -> PullRequestContext:
    """Fetch PR metadata and the unified diff."""
    with tracer.start_as_current_span("github.fetch_pr_context") as span:
        span.set_attribute("github.repo", repo_full_name)
        span.set_attribute("github.pr_number", pr_number)

        gh = _get_github_client()
        repo: Repository = gh.get_repo(repo_full_name)
        pr: PullRequest = repo.get_pull(pr_number)

        # Fetch raw diff via Accept header
        diff_text = _fetch_raw_diff(repo_full_name, pr_number)

        changed_files = [f.filename for f in pr.get_files()]

        return PullRequestContext(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            base_sha=pr.base.sha,
            head_sha=pr.head.sha,
            diff=diff_text,
            changed_files=changed_files,
            title=pr.title,
            body=pr.body or "",
        )


def _fetch_raw_diff(repo_full_name: str, pr_number: int) -> str:
    """Fetch unified diff using GitHub's diff media type."""
    token = _settings.github_token
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "Authorization": f"Bearer {token}",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def post_review_comments(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    comments: list[InlineComment],
    summary_body: str = "",
) -> None:
    """
    Post a PR review with inline comments and an optional summary.
    Groups everything into a single Review API call to avoid spam.
    """
    with tracer.start_as_current_span("github.post_review") as span:
        span.set_attribute("github.repo", repo_full_name)
        span.set_attribute("github.pr_number", pr_number)
        span.set_attribute("github.comment_count", len(comments))

        gh = _get_github_client()
        repo: Repository = gh.get_repo(repo_full_name)
        pr: PullRequest = repo.get_pull(pr_number)

        review_comments = [
            {
                "path": c.path,
                "line": c.line,
                "side": c.side,
                "body": c.body,
            }
            for c in comments
        ]

        pr.create_review(
            commit=repo.get_commit(commit_sha),
            body=summary_body or "🤖 **AI Code Review** — automated analysis complete.",
            event="COMMENT",
            comments=review_comments,
        )
        logger.info("Posted %d inline comment(s) on PR #%d.", len(comments), pr_number)


def post_pr_comment(repo_full_name: str, pr_number: int, body: str) -> None:
    """Post a general (non-inline) comment on a PR."""
    gh = _get_github_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    pr.create_issue_comment(body)


def create_commit(
    repo_full_name: str,
    branch: str,
    file_path: str,
    content: str,
    commit_message: str,
) -> str:
    """Create or update a file in a branch and return the new commit SHA."""
    with tracer.start_as_current_span("github.create_commit") as span:
        span.set_attribute("github.repo", repo_full_name)
        span.set_attribute("github.branch", branch)

        gh = _get_github_client()
        repo = gh.get_repo(repo_full_name)

        try:
            existing = repo.get_contents(file_path, ref=branch)
            result = repo.update_file(
                path=file_path,
                message=commit_message,
                content=content,
                sha=existing.sha,
                branch=branch,
            )
        except Exception:
            result = repo.create_file(
                path=file_path,
                message=commit_message,
                content=content,
                branch=branch,
            )

        sha = result["commit"].sha
        span.set_attribute("github.commit_sha", sha)
        return sha


def get_file_content(repo_full_name: str, file_path: str, ref: str) -> str:
    """Return decoded file content at a given git ref."""
    gh = _get_github_client()
    repo = gh.get_repo(repo_full_name)
    contents = repo.get_contents(file_path, ref=ref)
    if isinstance(contents, list):
        raise ValueError(f"'{file_path}' is a directory, not a file.")
    return base64.b64decode(contents.content).decode("utf-8", errors="replace")
