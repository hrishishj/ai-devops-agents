"""Verify who an approver is using GitHub itself (free): token -> login -> write access to a repo."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class GitHubIdentity:
    login: str
    can_write: bool


class GitHubIdentityVerifier:
    """Resolves a user's GitHub token to a login and checks they can push to `repo`.

    The token is only ever sent to GitHub. Results are cached in memory for `ttl` seconds, keyed by a
    SHA-256 of the token (the token itself is never retained), and nothing is logged or persisted.
    """

    def __init__(
        self,
        api_url: str = "https://api.github.com",
        *,
        ttl: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = httpx.Client(base_url=api_url, timeout=15.0, transport=transport)
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, GitHubIdentity | None]] = {}

    def verify(self, token: str, repo: str) -> GitHubIdentity | None:
        """None = the token is invalid or the repo is not visible to it."""
        key = (hashlib.sha256(token.encode()).hexdigest(), repo)
        hit = self._cache.get(key)
        if hit and hit[0] > self._clock():
            return hit[1]
        ident = self._lookup(token, repo)
        self._cache[key] = (self._clock() + self._ttl, ident)
        if len(self._cache) > 256:  # bound memory
            self._cache.clear()
        return ident

    def _lookup(self, token: str, repo: str) -> GitHubIdentity | None:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        try:
            u = self._http.get("/user", headers=headers)
            if u.status_code != 200:
                return None
            login = str(u.json().get("login", ""))
            r = self._http.get(f"/repos/{repo}", headers=headers)
        except httpx.HTTPError:
            return None
        if not login or r.status_code != 200:
            return None
        perms = r.json().get("permissions") or {}
        return GitHubIdentity(login, bool(perms.get("push") or perms.get("maintain") or perms.get("admin")))
