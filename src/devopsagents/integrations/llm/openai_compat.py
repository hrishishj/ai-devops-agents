"""OpenAI-compatible chat-completions backend (plain httpx, no SDK).

One adapter covers every free option that speaks this protocol:

  Ollama (local)   http://localhost:11434/v1                   (no key; private)
  Groq             https://api.groq.com/openai/v1              (free-tier key)
  OpenRouter       https://openrouter.ai/api/v1                (free ":free" models; low daily cap)
  Mistral          https://api.mistral.ai/v1                   (free "Experiment" plan)
  Gemini (compat)  https://generativelanguage.googleapis.com/v1beta/openai
  LM Studio        http://localhost:1234/v1

(GitHub Models, https://models.github.ai/inference, was retired on 2026-07-30 and no longer works.)
"""

from __future__ import annotations

import httpx

from devopsagents.core.llm.gateway import Completion, ModelTier, TransientLLMError


def _retry_after(r: httpx.Response) -> float | None:
    raw = r.headers.get("retry-after")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class OpenAICompatBackend:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_large: str,
        model_small: str,
        *,
        json_mode: bool = True,
        timeout: float = 300.0,  # local models can be slow on CPU
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._models = {ModelTier.LARGE: model_large, ModelTier.SMALL: model_small}
        self._json_mode = json_mode
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def complete(
        self,
        *,
        tier: ModelTier,
        system: str,
        user: str,
        json_schema: dict[str, object],
        max_tokens: int,
    ) -> Completion:
        body: dict[str, object] = {
            "model": self._models[tier],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = self._http.post(self._url, headers=self._headers, json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientLLMError(f"transport error: {exc}") from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientLLMError(f"HTTP {r.status_code}: {r.text[:200]}", _retry_after(r))
        if r.status_code >= 400:
            # permanent (bad key, unknown model, unsupported parameter): fail loudly, do not retry
            raise RuntimeError(f"LLM request rejected (HTTP {r.status_code}): {r.text[:300]}")
        data = r.json()
        content = (data["choices"][0]["message"].get("content")) or ""
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or (len(system) + len(user)) // 4)
        completion_tokens = int(usage.get("completion_tokens") or len(content) // 4)
        return Completion(content, prompt_tokens, completion_tokens, str(data.get("model", "")))
