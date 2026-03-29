"""
Shared Azure OpenAI client with retry logic, exponential back-off,
token budget enforcement, and OpenTelemetry tracing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from openai import AzureOpenAI, RateLimitError, APIStatusError
from opentelemetry import trace
from opentelemetry.trace import Span

from shared.config import Settings
from shared.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

_settings = Settings()

# --------------------------------------------------------------------------- #
# Data Structures
# --------------------------------------------------------------------------- #

@dataclass
class LLMRequest:
    system_prompt: str
    user_message: str
    temperature: float = 0.2
    max_tokens: int = 2048
    response_format: dict[str, str] | None = None   # e.g. {"type": "json_object"}
    few_shot_examples: list[dict[str, str]] = field(default_factory=list)


@dataclass
class LLMResponse:
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    finish_reason: str
    latency_ms: float


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class AzureOpenAIClient:
    """
    Thin, opinionated wrapper around the Azure OpenAI SDK.

    Features
    --------
    * Managed Identity / API-key auth (key resolved from Key Vault at startup).
    * Exponential back-off on 429 / transient 5xx errors.
    * Per-call token budget guard (raises if max_tokens > configured ceiling).
    * OpenTelemetry spans with token usage attributes.
    """

    _MAX_RETRIES = 5
    _BACKOFF_BASE = 1.5   # seconds
    _TOKEN_CEILING = 4096  # hard cap per request

    def __init__(self) -> None:
        self._client = AzureOpenAI(
            api_key=_settings.azure_openai_api_key,
            azure_endpoint=_settings.azure_openai_endpoint,
            api_version=_settings.azure_openai_api_version,
        )
        self._deployment = _settings.azure_openai_deployment

    # ---------------------------------------------------------------------- #
    # Public
    # ---------------------------------------------------------------------- #

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a chat completion request and return a structured response."""
        self._validate_token_budget(request.max_tokens)
        messages = self._build_messages(request)

        with tracer.start_as_current_span("llm.complete") as span:
            span.set_attribute("llm.deployment", self._deployment)
            span.set_attribute("llm.max_tokens", request.max_tokens)
            span.set_attribute("llm.temperature", request.temperature)

            response = self._call_with_retry(request, messages, span)

        return response

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    def _build_messages(self, request: LLMRequest) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": request.system_prompt}
        ]
        for example in request.few_shot_examples:
            messages.append({"role": "user", "content": example["user"]})
            messages.append({"role": "assistant", "content": example["assistant"]})
        messages.append({"role": "user", "content": request.user_message})
        return messages

    def _call_with_retry(
        self,
        request: LLMRequest,
        messages: list[dict[str, str]],
        span: Span,
    ) -> LLMResponse:
        extra_kwargs: dict[str, Any] = {}
        if request.response_format:
            extra_kwargs["response_format"] = request.response_format

        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES):
            try:
                t0 = time.monotonic()
                raw = self._client.chat.completions.create(
                    model=self._deployment,
                    messages=messages,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    **extra_kwargs,
                )
                latency_ms = (time.monotonic() - t0) * 1000

                usage = raw.usage
                span.set_attribute("llm.prompt_tokens", usage.prompt_tokens)
                span.set_attribute("llm.completion_tokens", usage.completion_tokens)
                span.set_attribute("llm.total_tokens", usage.total_tokens)
                span.set_attribute("llm.latency_ms", round(latency_ms, 2))

                return LLMResponse(
                    content=raw.choices[0].message.content or "",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    model=raw.model,
                    finish_reason=raw.choices[0].finish_reason,
                    latency_ms=latency_ms,
                )

            except RateLimitError as exc:
                last_exc = exc
                wait = self._backoff(attempt)
                logger.warning("Rate-limited by Azure OpenAI. Waiting %.1fs (attempt %d).", wait, attempt + 1)
                time.sleep(wait)

            except APIStatusError as exc:
                if exc.status_code >= 500:
                    last_exc = exc
                    wait = self._backoff(attempt)
                    logger.warning("Azure OpenAI 5xx error %d. Retrying in %.1fs.", exc.status_code, wait)
                    time.sleep(wait)
                else:
                    raise

        raise RuntimeError(
            f"Azure OpenAI call failed after {self._MAX_RETRIES} retries."
        ) from last_exc

    @staticmethod
    def _backoff(attempt: int) -> float:
        return AzureOpenAIClient._BACKOFF_BASE ** (attempt + 1)

    def _validate_token_budget(self, max_tokens: int) -> None:
        if max_tokens > self._TOKEN_CEILING:
            raise ValueError(
                f"Requested max_tokens={max_tokens} exceeds ceiling {self._TOKEN_CEILING}."
            )


# Singleton for import convenience
_client_singleton: AzureOpenAIClient | None = None


def get_llm_client() -> AzureOpenAIClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = AzureOpenAIClient()
    return _client_singleton
