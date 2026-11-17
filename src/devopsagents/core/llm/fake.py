"""Deterministic LLM backend for tests, evals and offline dry-runs."""

from __future__ import annotations

import json
from collections.abc import Callable

from devopsagents.core.llm.gateway import Completion, ModelTier


class ScriptedBackend:
    """Replies come from a callable (system, user) -> dict|str, or a FIFO list of replies."""

    def __init__(
        self,
        responder: Callable[[str, str], dict[str, object] | str] | None = None,
        replies: list[dict[str, object] | str | Exception] | None = None,
    ) -> None:
        self._responder = responder
        self._replies = list(replies or [])
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        *,
        tier: ModelTier,
        system: str,
        user: str,
        json_schema: dict[str, object],
        max_tokens: int,
    ) -> Completion:
        self.calls.append({"tier": tier, "system": system, "user": user, "max_tokens": max_tokens})
        if self._replies:
            reply = self._replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
        elif self._responder:
            reply = self._responder(system, user)
        else:
            raise AssertionError("ScriptedBackend has no reply left")
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return Completion(
            content=content,
            prompt_tokens=len(user) // 4 + 10,
            completion_tokens=len(content) // 4 + 5,
            model="fake",
        )
