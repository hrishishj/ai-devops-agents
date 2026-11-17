"""GitHub App client (httpx). Installation tokens are minted per installation and cached."""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt


@dataclass(frozen=True)
class PullRequestInfo:
    repo: str
    number: int
    title: str
    body: str
    head_sha: str
    base_sha: str


@dataclass(frozen=True)
class ReviewComment:
    path: str
    line: int
    body: str


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHubClient:
    def __init__(
        self,
        app_id: str,
        private_key: str,
        *,
        api_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._key = private_key.replace("\\n", "\n")
        self._http = httpx.Client(base_url=api_url, transport=transport, timeout=30.0)
        self._clock = clock
        self._tokens: dict[int, tuple[str, float]] = {}

    # ---- auth ---------------------------------------------------------------------------
    def _app_jwt(self) -> str:
        now = int(self._clock())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._key, algorithm="RS256"
        )

    def _installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] - 60 > self._clock():
            return cached[0]
        r = self._http.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._app_jwt()}", "Accept": "application/vnd.github+json"},
        )
        self._raise(r)
        data = r.json()
        # expires_at is ISO8601; refresh conservatively after 50 minutes
        self._tokens[installation_id] = (data["token"], self._clock() + 3000)
        return str(data["token"])

    def _req(
        self, installation_id: int, method: str, url: str, *, accept: str | None = None, **kw: Any
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._installation_token(installation_id)}",
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        r = self._http.request(method, url, headers=headers, **kw)
        self._raise(r)
        return r

    @staticmethod
    def _raise(r: httpx.Response) -> None:
        if r.status_code >= 400:
            raise GitHubError(r.status_code, r.text[:300])

    # ---- reads --------------------------------------------------------------------------
    def get_pr(self, inst: int, repo: str, number: int) -> PullRequestInfo:
        d = self._req(inst, "GET", f"/repos/{repo}/pulls/{number}").json()
        return PullRequestInfo(
            repo, number, d["title"], d.get("body") or "", d["head"]["sha"], d["base"]["sha"]
        )

    def get_diff(self, inst: int, repo: str, number: int) -> str:
        return self._req(
            inst, "GET", f"/repos/{repo}/pulls/{number}", accept="application/vnd.github.v3.diff"
        ).text

    def get_file(self, inst: int, repo: str, path: str, ref: str) -> str | None:
        try:
            r = self._req(inst, "GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        d = r.json()
        if isinstance(d, list) or d.get("encoding") != "base64":
            return None
        return base64.b64decode(d["content"]).decode("utf-8", errors="replace")

    # ---- writes -------------------------------------------------------------------------
    def post_review(
        self, inst: int, repo: str, number: int, commit_sha: str, body: str, comments: list[ReviewComment]
    ) -> int:
        payload = {
            "commit_id": commit_sha,
            "body": body,
            "event": "COMMENT",
            "comments": [{"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body} for c in comments],
        }
        r = self._req(inst, "POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload)
        return int(r.json()["id"])

    def post_comment(self, inst: int, repo: str, number: int, body: str) -> None:
        self._req(inst, "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    def create_issue(self, inst: int, repo: str, title: str, body: str, labels: list[str]) -> str:
        r = self._req(
            inst,
            "POST",
            f"/repos/{repo}/issues",
            json={"title": title[:250], "body": body[:60000], "labels": labels},
        )
        return str(r.json().get("html_url", ""))

    def create_check_run(
        self,
        inst: int,
        repo: str,
        head_sha: str,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
        annotations: list[dict[str, Any]],
    ) -> None:
        # GitHub allows max 50 annotations per request
        self._req(
            inst,
            "POST",
            f"/repos/{repo}/check-runs",
            json={
                "name": name,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": title, "summary": summary[:60000], "annotations": annotations[:50]},
            },
        )
