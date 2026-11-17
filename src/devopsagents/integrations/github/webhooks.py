"""GitHub webhook signature verification and payload normalisation."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from devopsagents.domain.events import PullRequestEvent

_PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Fail closed: an empty secret or a missing/malformed header is never valid."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_pull_request(delivery_id: str, payload: dict[str, Any]) -> PullRequestEvent | None:
    action = payload.get("action", "")
    pr = payload.get("pull_request") or {}
    if action not in _PR_ACTIONS or not pr or (pr.get("draft") and action != "ready_for_review"):
        return None
    return PullRequestEvent(
        delivery_id=delivery_id,
        repo=payload["repository"]["full_name"],
        number=int(pr["number"]),
        head_sha=pr["head"]["sha"],
        base_sha=pr["base"]["sha"],
        installation_id=(payload.get("installation") or {}).get("id"),
        action=action,
    )
