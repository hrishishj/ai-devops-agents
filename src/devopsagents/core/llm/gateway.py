"""LLM gateway: redaction, budgets, caching, retries, schema-validated structured output.

Every agent talks to models only through this class. Backends (Azure OpenAI, fakes) implement
the tiny `LLMBackend` protocol.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from devopsagents.core.jobs.store import JobStore
from devopsagents.core.llm.redact import redact

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ModelTier(StrEnum):
    SMALL = "small"  # classification, short summaries
    LARGE = "large"  # reasoning over code / incidents


@dataclass(frozen=True)
class Completion:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str = ""


class LLMBackend(Protocol):
    def complete(
        self,
        *,
        tier: ModelTier,
        system: str,
        user: str,
        json_schema: dict[str, object],
        max_tokens: int,
    ) -> Completion: ...


class TransientLLMError(Exception):
    """Rate limits / 5xx — the gateway retries these with backoff."""


class BudgetExceeded(Exception):
    pass


class LLMOutputError(Exception):
    """Model output failed schema validation even after one repair attempt."""


class KillSwitchActive(Exception):
    pass


class _LRU:
    def __init__(self, size: int = 256) -> None:
        self._d: OrderedDict[str, str] = OrderedDict()
        self._size = size

    def get(self, k: str) -> str | None:
        if k in self._d:
            self._d.move_to_end(k)
            return self._d[k]
        return None

    def put(self, k: str, v: str) -> None:
        self._d[k] = v
        self._d.move_to_end(k)
        while len(self._d) > self._size:
            self._d.popitem(last=False)


class LLMGateway:
    def __init__(
        self,
        backend: LLMBackend,
        store: JobStore,
        *,
        per_scope_budget: int = 2_000_000,
        global_budget: int = 20_000_000,
        max_output_tokens: int = 4096,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        kill_switch: Callable[[], bool] = lambda: False,
    ) -> None:
        self._backend = backend
        self._store = store
        self._scope_budget = per_scope_budget
        self._global_budget = global_budget
        self._max_out = max_output_tokens
        self._retries = max_retries
        self._backoff = backoff_base
        self._sleep = sleep
        self._kill = kill_switch
        self._cache = _LRU()

    def structured(
        self,
        *,
        tier: ModelTier,
        system: str,
        user: str,
        response_model: type[T],
        scope: str,
        prompt_id: str = "",
        max_tokens: int = 2048,
        cache: bool = True,
    ) -> T:
        if self._kill():
            raise KillSwitchActive("global kill switch is on")
        self._check_budget(scope)

        system, user = redact(system), redact(user)  # nothing secret leaves the service
        max_tokens = min(max_tokens, self._max_out)
        schema = response_model.model_json_schema()
        system = (
            f"{system}\n\nRespond with a single JSON object matching this JSON Schema:\n{json.dumps(schema)}"
        )
        ckey = hashlib.sha256(
            json.dumps([prompt_id, tier.value, system, user, schema], sort_keys=True).encode()
        ).hexdigest()

        if cache and (hit := self._cache.get(ckey)) is not None:
            try:
                return response_model.model_validate_json(hit)
            except ValidationError:
                pass

        comp = self._call(tier, system, user, schema, max_tokens, scope)
        try:
            parsed = response_model.model_validate_json(comp.content)
        except ValidationError as first:
            # one repair attempt: show the model its own error
            repair_user = (
                f"{user}\n\nYour previous reply was invalid:\n{str(first)[:1500]}\n"
                "Return ONLY JSON that satisfies the schema."
            )
            comp = self._call(tier, system, repair_user, schema, max_tokens, scope)
            try:
                parsed = response_model.model_validate_json(comp.content)
            except ValidationError as second:
                raise LLMOutputError(f"invalid model output: {second}") from second
        if cache:
            self._cache.put(ckey, parsed.model_dump_json())
        return parsed

    # ------------------------------------------------------------------------------------
    def _call(
        self,
        tier: ModelTier,
        system: str,
        user: str,
        schema: dict[str, object],
        max_tokens: int,
        scope: str,
    ) -> Completion:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                comp = self._backend.complete(
                    tier=tier, system=system, user=user, json_schema=schema, max_tokens=max_tokens
                )
                total = comp.prompt_tokens + comp.completion_tokens
                self._store.record_usage(scope, total)
                self._store.record_usage("global", total)
                return comp
            except TransientLLMError as exc:
                last = exc
                if attempt < self._retries:
                    self._sleep(self._backoff * (2**attempt))
        raise TransientLLMError(f"LLM unavailable after {self._retries + 1} attempts") from last

    def _check_budget(self, scope: str) -> None:
        if self._store.usage_today(scope) >= self._scope_budget:
            raise BudgetExceeded(f"daily token budget exhausted for {scope}")
        if self._store.usage_today("global") >= self._global_budget:
            raise BudgetExceeded("global daily token budget exhausted")
