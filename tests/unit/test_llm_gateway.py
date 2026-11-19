"""LLM gateway (ADR-005): request shape per provider, <think> stripping, JSON + 1 retry."""

import json

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from doc_intel.core.config import Settings, get_settings
from doc_intel.llm.gateway import (
    LLMError,
    OllamaClient,
    OpenAICompatClient,
    complete_json,
    make_llm_client,
    strip_think,
)
from tests.fakes import FakeLLM

MESSAGES = [{"role": "user", "content": "secret document text"}]
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


class Out(BaseModel):
    answer: str


def _mock(requests: list[httpx.Request], reply: dict, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ollama(http: httpx.AsyncClient, think: bool = False) -> OllamaClient:
    return OllamaClient(http, "http://ollama:11434/", "qwen3:14b", 8192, 120.0, think)


def _openai(http: httpx.AsyncClient, key: str = "sk-test") -> OpenAICompatClient:
    return OpenAICompatClient(http, "https://api.example.com/v1/", "gpt-x", SecretStr(key), 120.0)


async def test_ollama_uses_native_chat_with_num_ctx_think_off_and_schema_format():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"message": {"role": "assistant", "content": '{"answer": "x"}'}})
    assert await _ollama(http).complete(MESSAGES, SCHEMA) == '{"answer": "x"}'
    [req] = requests
    body = json.loads(req.content)
    assert str(req.url) == "http://ollama:11434/api/chat"
    assert body == {
        "model": "qwen3:14b",
        "messages": MESSAGES,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"num_ctx": 8192, "temperature": 0},
    }


async def test_ollama_plain_text_sends_no_format_and_can_enable_thinking():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"message": {"content": "hi"}})
    await _ollama(http, think=True).complete(MESSAGES)
    body = json.loads(requests[0].content)
    assert "format" not in body
    assert body["think"] is True


async def test_openai_compat_sends_bearer_key_and_json_schema_response_format():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"choices": [{"message": {"content": '{"answer": "x"}'}}]})
    assert await _openai(http).complete(MESSAGES, SCHEMA) == '{"answer": "x"}'
    [req] = requests
    body = json.loads(req.content)
    assert str(req.url) == "https://api.example.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    assert body["temperature"] == 0
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "output", "schema": SCHEMA},
    }


async def test_openai_compat_without_key_sends_no_auth_header():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"choices": [{"message": {"content": "hi"}}]})
    await _openai(http, key="").complete(MESSAGES)
    assert "authorization" not in requests[0].headers
    assert "response_format" not in json.loads(requests[0].content)


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_http_errors_raise_llm_error_without_text_or_key(status):
    for client in (_ollama(_mock([], {"error": "boom"}, status)), _openai(_mock([], {}, status))):
        with pytest.raises(LLMError) as exc:
            await client.complete(MESSAGES)
        assert exc.value.code == "upstream_unavailable"
        assert "secret document text" not in str(exc.value)
        assert "sk-test" not in str(exc.value)


@pytest.mark.parametrize("reply", [{}, {"message": None}, {"choices": []}])
async def test_malformed_responses_raise_llm_error(reply):
    for client in (_ollama(_mock([], reply)), _openai(_mock([], reply))):
        with pytest.raises(LLMError):
            await client.complete(MESSAGES)


async def test_unreachable_server_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        await _ollama(http).complete(MESSAGES)


@pytest.mark.parametrize(
    "raw, clean",
    [
        ('{"a": 1}', '{"a": 1}'),
        ('<think>\nlet me think\n</think>\n\n{"a": 1}', '{"a": 1}'),
        ("<think></think>Answer", "Answer"),
        # A quoted document may contain the tag: only a leading reasoning block is removed.
        ('{"quote": "close it with </think> here"}', '{"quote": "close it with </think> here"}'),
        ('<think>x</think>{"quote": "a </think> b"}', '{"quote": "a </think> b"}'),
    ],
)
def test_strip_think(raw, clean):
    assert strip_think(raw) == clean


async def test_complete_json_strips_think_and_validates():
    llm = FakeLLM('<think>hmm</think>{"answer": "Paris"}')
    assert await complete_json(llm, MESSAGES, Out) == Out(answer="Paris")
    [(messages, schema)] = llm.calls
    assert messages == MESSAGES
    assert schema == Out.model_json_schema()


async def test_complete_json_retries_once_with_a_correction_then_succeeds():
    llm = FakeLLM("not json", '{"answer": "Paris"}')
    assert await complete_json(llm, MESSAGES, Out) == Out(answer="Paris")
    retry_messages = llm.calls[1][0]
    assert retry_messages[: len(MESSAGES)] == MESSAGES
    assert retry_messages[-2] == {"role": "assistant", "content": "not json"}
    assert retry_messages[-1]["role"] == "user"


async def test_complete_json_fails_after_two_invalid_replies():
    llm = FakeLLM("not json", '{"wrong": 1}')
    with pytest.raises(LLMError):
        await complete_json(llm, MESSAGES, Out)
    assert len(llm.calls) == 2


def test_make_llm_client_picks_the_provider():
    base = {"_env_file": None, "pipe_shared_secret": "x" * 32, "signing_secret": "y" * 32}
    http = httpx.AsyncClient()
    assert isinstance(make_llm_client(Settings(**base), http), OllamaClient)
    openai = Settings(**base, llm_provider="openai", llm_base_url="https://api.example.com/v1")
    assert isinstance(make_llm_client(openai, http), OpenAICompatClient)


def test_unknown_provider_fails_at_startup():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            pipe_shared_secret="x" * 32,
            signing_secret="y" * 32,
            llm_provider="other",
        )


@pytest.mark.llm
async def test_qwen3_live_json_without_think_tags():
    settings = get_settings()
    async with httpx.AsyncClient() as http:
        llm = make_llm_client(settings, http)
        raw = await llm.complete(
            [{"role": "user", "content": "What is the capital of France? Reply as JSON."}],
            Out.model_json_schema(),
        )
        assert "<think>" not in raw
        assert "paris" in Out.model_validate_json(raw).answer.lower()
