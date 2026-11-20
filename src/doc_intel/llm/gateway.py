"""Provider-agnostic LLM gateway (ADR-005): Ollama native API by default, OpenAI-compatible else.

Ollama is called on `/api/chat`, not its OpenAI-compatible `/v1`: `/v1` ignores `num_ctx`
and runs at 4096 tokens, which would silently cut the start (the instructions) of a Q&A prompt.
"""

import re
from collections.abc import Callable
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from doc_intel.core.config import Settings
from doc_intel.core.errors import UpstreamUnavailable

Messages = list[dict[str, str]]
_THINK = re.compile(r"\A\s*<think>.*?</think>", re.DOTALL)
RETRY_ECHO_CHARS = 1500  # a failed reply is echoed truncated, so a retry never outgrows num_ctx
RETRY_PROMPT = "That reply was not valid JSON for the required schema. Reply with only the JSON."


class LLMError(UpstreamUnavailable):
    message = "The language model is unavailable or returned invalid output."


class LLMInvalidOutput(LLMError):
    """The model replied twice with invalid output (transport failures stay plain `LLMError`)."""

    retry = False  # jobs: temperature 0, the same prompt fails the same way


class LLMClient(Protocol):
    async def complete(self, messages: Messages, json_schema: dict | None = None) -> str:
        """The reply text; `json_schema` constrains it to JSON of that shape."""
        ...


async def _post(
    http: httpx.AsyncClient,
    url: str,
    body: dict,
    timeout_s: float,
    pick: Callable[[Any], str],
    headers: dict[str, str] | None = None,
) -> str:
    try:
        response = await http.post(url, json=body, headers=headers, timeout=timeout_s)
        response.raise_for_status()
        return pick(response.json())
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        # Type only: the request carried document text, the headers an API key.
        raise LLMError(f"LLM request failed ({type(exc).__name__}).") from exc


class OllamaClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        model: str,
        num_ctx: int,
        timeout_s: float,
        think: bool,
    ) -> None:
        self._http, self._model, self._timeout = http, model, timeout_s
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self._num_ctx, self._think = num_ctx, think

    async def complete(self, messages: Messages, json_schema: dict | None = None) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "think": self._think,
            # a reply may use at most half the window; uncapped, a degenerate JSON/whitespace loop
            # runs until the timeout
            "options": {
                "num_ctx": self._num_ctx,
                "temperature": 0,
                "num_predict": self._num_ctx // 2,
            },
        }
        if json_schema is not None:
            body["format"] = json_schema
        return await _post(
            self._http, self._url, body, self._timeout, lambda d: d["message"]["content"]
        )


class OpenAICompatClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        model: str,
        api_key: SecretStr,
        timeout_s: float,
    ) -> None:
        self._http, self._model, self._timeout = http, model, timeout_s
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        key = api_key.get_secret_value()
        self._headers = {"Authorization": f"Bearer {key}"} if key else None

    async def complete(self, messages: Messages, json_schema: dict | None = None) -> str:
        body: dict[str, Any] = {"model": self._model, "messages": messages, "temperature": 0}
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema},
            }
        return await _post(
            self._http,
            self._url,
            body,
            self._timeout,
            lambda d: d["choices"][0]["message"]["content"],
            self._headers,
        )


def make_llm_client(settings: Settings, http: httpx.AsyncClient) -> LLMClient:
    if settings.llm_provider == "ollama":
        return OllamaClient(
            http,
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_num_ctx,
            settings.llm_timeout_seconds,
            settings.llm_enable_thinking,
        )
    return OpenAICompatClient(
        http,
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key,
        settings.llm_timeout_seconds,
    )


def strip_think(text: str) -> str:
    """Drop a leading reasoning block; a `</think>` quoted from a document stays."""
    return _THINK.sub("", text, count=1).strip()


async def complete_json[M: BaseModel](llm: LLMClient, messages: Messages, model: type[M]) -> M:
    """Schema-constrained call validated by `model`; one retry, then `LLMInvalidOutput`."""
    schema = model.model_json_schema()
    attempt = messages
    for _ in range(2):
        reply = strip_think(await llm.complete(attempt, schema))
        try:
            return model.model_validate_json(reply)
        except ValidationError:
            # Rebuilt from the original messages: failed attempts never stack up.
            attempt = [
                *messages,
                {"role": "assistant", "content": reply[:RETRY_ECHO_CHARS]},
                {"role": "user", "content": RETRY_PROMPT},
            ]
    raise LLMInvalidOutput("The language model returned invalid output twice.")
