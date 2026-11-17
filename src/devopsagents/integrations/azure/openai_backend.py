"""Azure OpenAI backend (Entra ID auth, no API keys). Requires the `azure` extra."""

from __future__ import annotations

from devopsagents.core.llm.gateway import Completion, ModelTier, TransientLLMError


class AzureOpenAIBackend:  # pragma: no cover - exercised only against live Azure
    def __init__(self, endpoint: str, api_version: str, model_large: str, model_small: str) -> None:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI

        token = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_version=api_version,
            azure_ad_token_provider=token,
            max_retries=0,  # retries are owned by the gateway
        )
        self._models = {ModelTier.LARGE: model_large, ModelTier.SMALL: model_small}

    def complete(
        self,
        *,
        tier: ModelTier,
        system: str,
        user: str,
        json_schema: dict[str, object],
        max_tokens: int,
    ) -> Completion:
        import openai

        try:
            r = self._client.chat.completions.create(
                model=self._models[tier],
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=max_tokens,
                temperature=0,
                response_format={"type": "json_object"},
            )
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as e:
            raise TransientLLMError(str(e)) from e
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientLLMError(str(e)) from e
            raise
        u = r.usage
        return Completion(
            content=r.choices[0].message.content or "",
            prompt_tokens=u.prompt_tokens if u else 0,
            completion_tokens=u.completion_tokens if u else 0,
            model=r.model,
        )
