"""Provider-agnostic LLM gateway (ADR-005): Ollama native API by default, else `openai_compat`.

`openai_compat` is mlx_lm.server, LM Studio, vLLM or any OpenAI-compatible /v1 server.

Ollama is called on `/api/chat`, not its OpenAI-compatible `/v1`: `/v1` ignores `num_ctx`
and runs at 4096 tokens, which would silently cut the start (the instructions) of a Q&A prompt.
"""

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

import httpx
import structlog
from pydantic import BaseModel, SecretStr, ValidationError

from doc_intel.core.config import Settings
from doc_intel.core.errors import UpstreamUnavailable

log = structlog.get_logger()
Messages = list[dict[str, str]]
_THINK = re.compile(r"\A\s*<think>.*?</think>", re.DOTALL)
RETRY_ECHO_CHARS = 1500  # a failed reply is echoed truncated, so a retry never outgrows num_ctx
REPLY_TOKENS = 4096  # every reply fits; the cap stops a degenerate JSON/whitespace loop early
# shortcut: ir_builder and Q&A stay sized for the 8k floor, upgrade if they need the big window.


def prompt_tokens(num_ctx: int) -> int:
    """Token budget for one prompt (>= 6144); the rest of the window is reasoning plus reply."""
    return max(6144, num_ctx // 2)


def reply_tokens(num_ctx: int, think: bool) -> int:
    """Reply cap. Uncapped, a degenerate JSON/whitespace loop runs until the timeout. Reasoning
    counts against the same cap, so with thinking on the reply gets all the window the prompt
    leaves, else a long reasoning cuts the JSON off."""
    cap = min(num_ctx // 2, REPLY_TOKENS)
    return max(cap, num_ctx - prompt_tokens(num_ctx)) if think else cap


RETRY_PROMPT = "That reply was not valid JSON for the required schema. Reply with only the JSON."
MAX_RETRY_ERRORS = 10  # a reply missing every field still gets a short, fixable list
# Servers that ignore response_format (mlx_lm.server) see the schema only in the prompt.
SCHEMA_PROMPT = "Reply with only one JSON object, no markdown or prose, matching this JSON schema:"


def _retry_prompt(exc: ValidationError) -> str:
    """RETRY_PROMPT plus each error's field path and message; never the input values, which can
    be document text."""
    lines = [
        f"- {'.'.join(map(str, e['loc'])) or '(reply)'}: {e['msg']}"
        for e in exc.errors(include_url=False, include_input=False)[:MAX_RETRY_ERRORS]
    ]
    return "\n".join([RETRY_PROMPT, "Errors:", *lines])


def with_schema(messages: Messages, schema: dict) -> Messages:
    """The messages with the schema instruction appended to the last user turn (a copy)."""
    note = f"\n\n{SCHEMA_PROMPT}\n{json.dumps(schema, separators=(',', ':'))}"
    if messages and messages[-1]["role"] == "user":
        return [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + note}]
    return [*messages, {"role": "user", "content": note.lstrip()}]


_DECODER = json.JSONDecoder()


def extract_json(text: str) -> str:
    """The first JSON object in a reply wrapped in markdown fences or prose; the text unchanged
    when it holds none (validation then fails and names the problem)."""
    text = strip_think(text)
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            obj, end = _DECODER.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return text[start:end]
    return text


class LLMError(UpstreamUnavailable):
    message = "The language model is unavailable or returned invalid output."


class LLMInvalidOutput(LLMError):
    """The model replied twice with invalid output (transport failures stay plain `LLMError`)."""

    retry = False  # jobs: temperature 0, the same prompt fails the same way


class LLMClient(Protocol):
    async def complete(
        self, messages: Messages, json_schema: dict | None = None, think: bool = False
    ) -> str:
        """The reply text; `json_schema` constrains it to JSON of that shape; `think` asks for
        bounded reasoning where the provider supports it."""
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

    async def complete(
        self, messages: Messages, json_schema: dict | None = None, think: bool = False
    ) -> str:
        # shortcut: hybrid bounded thinking is openai_compat (MLX) only, add an Ollama streaming
        # guard if Ollama needs it.
        num_predict = reply_tokens(self._num_ctx, self._think)
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "think": self._think,
            "options": {"num_ctx": self._num_ctx, "temperature": 0, "num_predict": num_predict},
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
        num_ctx: int,
        timeout_s: float,
        think: bool,
        template_kwargs: bool,
        hybrid: bool,
        think_budget: int,
    ) -> None:
        self._http, self._model, self._timeout = http, model, timeout_s
        self._num_ctx, self._think, self._template_kwargs = num_ctx, think, template_kwargs
        self._hybrid, self._budget = hybrid, think_budget
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        key = api_key.get_secret_value()
        self._headers = {"Authorization": f"Bearer {key}"} if key else None

    def _body(
        self, messages: Messages, json_schema: dict | None, thinking: bool, max_tokens: int
    ) -> dict[str, Any]:
        # shortcut: the schema text costs prompt tokens beyond prompt_tokens(), taken from the
        # reply headroom; fine while schemas stay a few KB, budget it if a schema grows large.
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages if json_schema is None else with_schema(messages, json_schema),
            "temperature": 0,
            "max_tokens": max_tokens,  # mlx_lm.server defaults to 512
        }
        if self._template_kwargs:  # opt-in: strict OpenAI-compatible servers reject the field
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        if json_schema is not None:
            # Servers that ignore this (mlx_lm.server) are covered by complete_json's retry.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema},
            }
        return body

    async def _post_body(self, body: dict[str, Any]) -> str:
        return await _post(
            self._http,
            self._url,
            body,
            self._timeout,
            lambda d: d["choices"][0]["message"]["content"],
            self._headers,
        )

    async def _stream(self, body: dict[str, Any]) -> tuple[str, str, int, bool]:
        """(reasoning, answer, reasoning tokens, over budget); stops reading once over budget."""
        reasoning: list[str] = []
        content, tokens = "", 0
        try:
            async with self._http.stream(
                "POST", self._url, json=body, headers=self._headers, timeout=self._timeout
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    data = line[5:].strip() if line.startswith("data:") else ""
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    delta = json.loads(data)["choices"][0]["delta"]
                    thought = delta.get("reasoning") or delta.get("reasoning_content") or ""
                    content += delta.get("content") or ""
                    reasoning.append(thought)
                    inline = content.lstrip().startswith("<think>") and "</think>" not in content
                    if thought or (inline and delta.get("content")):
                        # shortcut: one SSE event ~ one token (mlx_lm streams per token), count
                        # with the tokenizer if a server batches deltas.
                        tokens += 1
                    if tokens > self._budget:
                        break  # leaving the context closes the connection
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM request failed ({type(exc).__name__}).") from exc
        over = tokens > self._budget
        partial = "".join(reasoning) + (re.sub(r"\A\s*<think>", "", content) if over else "")
        return partial, strip_think(content), tokens, over

    async def _bounded(self, messages: Messages, json_schema: dict | None) -> tuple[str, int, bool]:
        cap = reply_tokens(self._num_ctx, False)
        body = self._body(messages, json_schema, True, cap + self._budget)
        body["stream"] = True
        if self._template_kwargs:  # mlx_lm 0.32.2 ignores it, servers that support it honour it
            body["thinking_budget"] = self._budget
        partial, answer, tokens, over = await self._stream(body)
        if not over:
            return answer, tokens, False
        # mlx_lm cannot continue an assistant turn: one non-thinking pass, never the bounded path
        ask = (
            f"\n\nYour reasoning so far (cut at the thinking budget):\n{partial}\n"
            "Now reply with only the answer."
        )
        if messages and messages[-1]["role"] == "user":
            asked = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + ask}]
        else:
            asked = [*messages, {"role": "user", "content": ask.lstrip()}]
        return await self._post_body(self._body(asked, json_schema, False, cap)), tokens, True

    async def complete(
        self, messages: Messages, json_schema: dict | None = None, think: bool = False
    ) -> str:
        tokens, capped = 0, False
        if self._think:
            mode = "think"
        elif self._hybrid and think:
            mode = "bounded-think"
        else:
            mode = "fast"
        if mode == "bounded-think":
            reply, tokens, capped = await self._bounded(messages, json_schema)
        else:
            body = self._body(
                messages, json_schema, self._think, reply_tokens(self._num_ctx, self._think)
            )
            reply = await self._post_body(body)
        log.info("llm_call", mode=mode, think_tokens=tokens, capped=capped)
        return reply if json_schema is None else extract_json(reply)


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
        settings.llm_num_ctx,
        settings.llm_timeout_seconds,
        settings.llm_enable_thinking,
        settings.llm_chat_template_kwargs,
        settings.llm_hybrid_thinking,
        settings.llm_thinking_budget_tokens,
    )


def strip_think(text: str) -> str:
    """Drop a leading reasoning block; a `</think>` quoted from a document stays."""
    return _THINK.sub("", text, count=1).strip()


async def complete_json[M: BaseModel](
    llm: LLMClient, messages: Messages, model: type[M], think: bool = False
) -> M:
    """Schema-constrained call validated by `model`; one retry, then `LLMInvalidOutput`."""
    schema = model.model_json_schema()
    attempt = messages
    for _ in range(2):
        reply = strip_think(await llm.complete(attempt, schema, think=think))
        try:
            return model.model_validate_json(reply)
        except ValidationError as exc:
            # Rebuilt from the original messages: failed attempts never stack up.
            attempt = [
                *messages,
                {"role": "assistant", "content": reply[:RETRY_ECHO_CHARS]},
                {"role": "user", "content": _retry_prompt(exc)},
            ]
    raise LLMInvalidOutput("The language model returned invalid output twice.")
