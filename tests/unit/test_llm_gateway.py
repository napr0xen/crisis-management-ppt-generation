"""LLM gateway (ADR-005): request shape per provider, <think> stripping, JSON + 1 retry."""

import json

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from doc_intel.core.config import Settings, get_settings
from doc_intel.llm.gateway import (
    REPLY_TOKENS,
    RETRY_ECHO_CHARS,
    LLMError,
    LLMInvalidOutput,
    OllamaClient,
    OpenAICompatClient,
    complete_json,
    make_llm_client,
    strip_think,
    with_schema,
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


def _openai(
    http: httpx.AsyncClient,
    key: str = "sk-test",
    num_ctx: int = 32768,
    think: bool = False,
    template_kwargs: bool = False,
    hybrid: bool = False,
    budget: int = 3,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        http,
        "http://localhost:8080/v1/",
        "gpt-x",
        SecretStr(key),
        num_ctx,
        120.0,
        think,
        template_kwargs,
        hybrid,
        budget,
    )


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
        "options": {"num_ctx": 8192, "temperature": 0, "num_predict": 4096},
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
    assert str(req.url) == "http://localhost:8080/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    assert body == {
        "model": "gpt-x",
        "messages": with_schema(MESSAGES, SCHEMA),
        "temperature": 0,
        "max_tokens": 4096,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": SCHEMA},
        },
    }


async def test_openai_compat_thinking_gets_the_larger_budget():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"choices": [{"message": {"content": "hi"}}]})
    await _openai(http, think=True).complete(MESSAGES)
    body = json.loads(requests[0].content)
    assert body["max_tokens"] == 32768 - 16384
    assert "chat_template_kwargs" not in body  # strict OpenAI-compatible servers reject it
    assert "response_format" not in body


@pytest.mark.parametrize("think", [False, True])
async def test_openai_compat_sends_the_thinking_switch_only_when_configured(think):
    requests: list[httpx.Request] = []
    http = _mock(requests, {"choices": [{"message": {"content": "hi"}}]})
    await _openai(http, think=think, template_kwargs=True).complete(MESSAGES)
    assert json.loads(requests[0].content)["chat_template_kwargs"] == {"enable_thinking": think}


async def test_make_llm_client_passes_the_template_kwargs_setting(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("LLM_CHAT_TEMPLATE_KWARGS", "true")
    requests: list[httpx.Request] = []
    http = _mock(requests, {"choices": [{"message": {"content": "hi"}}]})
    settings = Settings(_env_file=None, pipe_shared_secret="x" * 32, signing_secret="y" * 32)
    await make_llm_client(settings, http).complete(MESSAGES)
    assert json.loads(requests[0].content)["chat_template_kwargs"] == {"enable_thinking": False}


def _scripted(contents: list[str], requests: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        reply = {"choices": [{"message": {"content": contents[len(requests) - 1]}}]}
        return httpx.Response(200, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openai_compat_complete_json_strips_think():
    requests: list[httpx.Request] = []
    http = _scripted(['<think>reasoning</think>\n{"answer": "x"}'], requests)
    assert await complete_json(_openai(http), MESSAGES, Out) == Out(answer="x")
    assert len(requests) == 1


async def test_openai_compat_server_ignoring_the_schema_once_is_retried():
    requests: list[httpx.Request] = []
    http = _scripted(["Sure! here you go", '{"answer": "x"}'], requests)
    assert await complete_json(_openai(http), MESSAGES, Out) == Out(answer="x")
    assert len(requests) == 2


async def test_openai_compat_two_invalid_replies_raise_invalid_output():
    requests: list[httpx.Request] = []
    http = _scripted(["nope", "still nope"], requests)
    with pytest.raises(LLMInvalidOutput):
        await complete_json(_openai(http), MESSAGES, Out)
    assert len(requests) == 2


async def test_openai_compat_unreachable_server_raises_llm_error_without_text_or_key():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError) as exc:
        await _openai(http).complete(MESSAGES)
    assert "secret document text" not in str(exc.value)
    assert "sk-test" not in str(exc.value)


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
        # reasoning that drafts JSON, with leading whitespace: only the final JSON is kept
        ('\n <think>\n{"a": 0}\n</think>\n{"a": 1}', '{"a": 1}'),
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


async def test_retry_echoes_a_truncated_reply_on_top_of_the_original_messages():
    llm = FakeLLM("x" * (RETRY_ECHO_CHARS + 500), '{"answer": "Paris"}')
    await complete_json(llm, MESSAGES, Out)
    retry = llm.calls[1][0]
    assert len(retry) == len(MESSAGES) + 2
    assert retry[-2] == {"role": "assistant", "content": "x" * RETRY_ECHO_CHARS}


async def test_failed_attempts_never_stack():
    llm = FakeLLM("bad", "worse")
    with pytest.raises(LLMError):
        await complete_json(llm, MESSAGES, Out)
    assert [len(m) for m, _ in llm.calls] == [len(MESSAGES), len(MESSAGES) + 2]


async def test_complete_json_fails_after_two_invalid_replies():
    llm = FakeLLM("not json", '{"wrong": 1}')
    with pytest.raises(LLMInvalidOutput):
        await complete_json(llm, MESSAGES, Out)
    assert len(llm.calls) == 2


def test_make_llm_client_picks_the_provider():
    base = {"_env_file": None, "pipe_shared_secret": "x" * 32, "signing_secret": "y" * 32}
    http = httpx.AsyncClient()
    assert isinstance(make_llm_client(Settings(**base), http), OllamaClient)
    compat = Settings(**base, llm_provider="openai_compat", llm_base_url="http://localhost:8080/v1")
    client = make_llm_client(compat, http)
    assert isinstance(client, OpenAICompatClient)
    assert (client._num_ctx, client._think) == (32768, False)
    with pytest.raises(ValueError):  # the old name has no alias
        Settings(**base, llm_provider="openai")


async def test_default_settings_send_a_32k_window_with_thinking_off():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"message": {"content": '{"answer": "x"}'}})
    settings = Settings(_env_file=None, pipe_shared_secret="x" * 32, signing_secret="y" * 32)
    await make_llm_client(settings, http).complete(MESSAGES, SCHEMA)
    body = json.loads(requests[0].content)
    assert body["think"] is False
    assert body["options"]["num_ctx"] == 32768
    assert body["options"]["num_predict"] == REPLY_TOKENS


def test_window_and_thinking_come_from_env(monkeypatch):
    monkeypatch.setenv("LLM_NUM_CTX", "8192")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "true")
    s = Settings(_env_file=None, pipe_shared_secret="x" * 32, signing_secret="y" * 32)
    assert (s.llm_num_ctx, s.llm_enable_thinking) == (8192, True)


@pytest.mark.parametrize(
    "num_ctx, think, num_predict",
    [
        (32768, False, REPLY_TOKENS),  # no reasoning: the degenerate-loop cap, as at 8k
        (32768, True, 32768 - 16384),  # reasoning + reply get all the prompt leaves
        (16384, True, 16384 - 8192),
        (8192, True, 4096),  # a small window never gives the reply less than half
    ],
)
async def test_thinking_gets_the_window_the_prompt_leaves(num_ctx, think, num_predict):
    requests: list[httpx.Request] = []
    http = _mock(requests, {"message": {"content": "hi"}})
    await OllamaClient(http, "http://o:11434", "m", num_ctx, 120.0, think).complete(MESSAGES)
    body = json.loads(requests[0].content)
    assert body["think"] is think
    assert body["options"]["num_ctx"] == num_ctx
    assert body["options"]["num_predict"] == num_predict


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


def test_invalid_output_is_an_llm_error_but_transport_errors_are_not_invalid_output():
    assert issubclass(LLMInvalidOutput, LLMError)
    assert not issubclass(LLMError, LLMInvalidOutput)


def test_invalid_output_is_not_retried_by_jobs_but_transport_errors_are():
    assert LLMInvalidOutput.retry is False
    assert getattr(LLMError(), "retry", True) is True


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"answer": "x"}\n```',  # markdown fence
        '```\n{"answer": "x"}\n```',  # fence without a language
        'Sure! Here is the JSON:\n{"answer": "x"}\nHope that helps.',  # prose around it
        '<think>plan {not json}</think>\n```json\n{"answer": "x"}\n```',  # reasoning, then fence
    ],
)
async def test_openai_compat_json_is_extracted_from_fences_and_prose(reply):
    requests: list[httpx.Request] = []
    http = _scripted([reply], requests)
    assert await complete_json(_openai(http), MESSAGES, Out) == Out(answer="x")
    assert len(requests) == 1


async def test_openai_compat_puts_the_schema_in_the_prompt_without_touching_the_caller_messages():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"choices": [{"message": {"content": '{"answer": "x"}'}}]})
    original = [dict(m) for m in MESSAGES]
    await _openai(http).complete(MESSAGES, SCHEMA)
    sent = json.loads(requests[0].content)["messages"]
    assert MESSAGES == original
    assert len(sent) == len(MESSAGES)
    assert sent[-1]["content"].startswith(MESSAGES[-1]["content"])
    assert json.dumps(SCHEMA, separators=(",", ":")) in sent[-1]["content"]


async def test_openai_compat_plain_text_reply_is_returned_as_is():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"choices": [{"message": {"content": "```py\nx = {1}\n```"}}]})
    assert await _openai(http).complete(MESSAGES) == "```py\nx = {1}\n```"
    assert json.loads(requests[0].content)["messages"] == MESSAGES


async def test_openai_compat_retry_names_the_failing_field():
    requests: list[httpx.Request] = []
    http = _scripted(['{"anwser": "x"}', '{"answer": "x"}'], requests)
    assert await complete_json(_openai(http), MESSAGES, Out) == Out(answer="x")
    retry = json.loads(requests[1].content)["messages"]
    assert retry[-2] == {"role": "assistant", "content": '{"anwser": "x"}'}
    feedback = retry[-1]["content"]
    assert "answer: Field required" in feedback
    assert "secret document text" not in feedback  # field paths and messages, not input values


async def test_retry_feedback_for_unparsable_json_says_so():
    llm = FakeLLM("not json", '{"answer": "Paris"}')
    await complete_json(llm, MESSAGES, Out)
    assert "Invalid JSON" in llm.calls[1][0][-1]["content"]


def _sse(*deltas: dict) -> bytes:
    lines = [f"data: {json.dumps({'choices': [{'delta': d}]})}\n\n" for d in deltas]
    return "".join([*lines, "data: [DONE]\n\n"]).encode()


def _streaming(requests: list[httpx.Request], stream: bytes, second: str = "{}"):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=stream)
        return httpx.Response(200, json={"choices": [{"message": {"content": second}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_think_flag_is_ignored_without_hybrid_and_by_ollama():
    requests: list[httpx.Request] = []
    http = _scripted(['{"answer": "x"}'], requests)
    await _openai(http, template_kwargs=True).complete(MESSAGES, SCHEMA, think=True)
    body = json.loads(requests[0].content)
    assert "stream" not in body and body["chat_template_kwargs"] == {"enable_thinking": False}
    requests.clear()
    http = _mock(requests, {"message": {"content": "x"}})
    await _ollama(http).complete(MESSAGES, think=True)
    assert json.loads(requests[0].content)["think"] is False


async def test_bounded_think_within_budget_streams_once_and_returns_the_answer():
    requests: list[httpx.Request] = []
    stream = _sse(
        {"reasoning": "a"},
        {"reasoning_content": "b"},
        {"content": '{"answer":'},
        {"content": '"x"}'},
    )
    http = _streaming(requests, stream)
    client = _openai(http, template_kwargs=True, hybrid=True)
    assert await client.complete(MESSAGES, SCHEMA, think=True) == '{"answer":"x"}'
    [req] = requests
    body = json.loads(req.content)
    assert body["stream"] is True and body["thinking_budget"] == 3
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["max_tokens"] == 4096 + 3


async def test_bounded_think_without_template_kwargs_sends_neither_field():
    requests: list[httpx.Request] = []
    http = _streaming(requests, _sse({"content": "ok"}))
    await _openai(http, hybrid=True).complete(MESSAGES, think=True)
    body = json.loads(requests[0].content)
    assert body["stream"] is True
    assert "chat_template_kwargs" not in body and "thinking_budget" not in body


async def test_inline_think_in_content_counts_as_reasoning():
    requests: list[httpx.Request] = []
    stream = _sse(
        {"content": "<think>"}, {"content": "a"}, {"content": "</think>"}, {"content": "hi"}
    )
    http = _streaming(requests, stream)
    assert await _openai(http, hybrid=True).complete(MESSAGES, think=True) == "hi"
    assert len(requests) == 1


async def test_bounded_think_overrun_makes_exactly_one_non_thinking_second_pass():
    requests: list[httpx.Request] = []
    stream = _sse(*({"reasoning": f"r{i} "} for i in range(50)))
    http = _streaming(requests, stream, second='{"answer": "x"}')
    client = _openai(http, template_kwargs=True, hybrid=True)
    assert await client.complete(MESSAGES, SCHEMA, think=True) == '{"answer": "x"}'
    assert len(requests) == 2
    second = json.loads(requests[1].content)
    assert "stream" not in second and "thinking_budget" not in second
    assert second["chat_template_kwargs"] == {"enable_thinking": False}
    assert second["max_tokens"] == 4096
    last = second["messages"][-1]["content"]
    assert "Your reasoning so far (cut at the thinking budget):\nr0 r1 r2 r3 " in last
    assert "Now reply with only the answer." in last


async def test_bounded_think_transport_error_is_an_llm_error():
    http = _mock([], {}, status=500)
    with pytest.raises(LLMError, match="HTTPStatusError") as exc:
        await _openai(http, hybrid=True).complete(MESSAGES, think=True)
    assert "secret" not in str(exc.value)


def test_make_llm_client_passes_hybrid_settings():
    s = Settings(
        llm_provider="openai_compat", llm_hybrid_thinking=True, llm_thinking_budget_tokens=7
    )
    client = make_llm_client(s, httpx.AsyncClient())
    assert client._hybrid is True and client._budget == 7


async def test_complete_json_passes_think_on_every_attempt():
    llm = FakeLLM("nope", '{"answer": "x"}')
    await complete_json(llm, MESSAGES, Out, think=True)
    assert llm.thinks == [True, True]


async def test_ollama_escalation_thinks_with_the_budget_on_top_of_the_reply_cap():
    requests: list[httpx.Request] = []
    http = _mock(requests, {"message": {"content": "x"}})
    await _ollama(http).complete(MESSAGES, SCHEMA, think=True, think_budget=2048)
    body = json.loads(requests[0].content)
    assert body["think"] is True
    assert body["options"]["num_predict"] == 4096 + 2048 >= 2048


async def test_openai_compat_escalation_bounds_thinking_without_hybrid():
    requests: list[httpx.Request] = []
    http = _streaming(requests, _sse({"reasoning": "a"}, {"content": '{"answer": "x"}'}))
    client = _openai(http, template_kwargs=True)  # hybrid off: only the escalation thinks
    assert await client.complete(MESSAGES, SCHEMA, think=True, think_budget=2048) == (
        '{"answer": "x"}'
    )
    body = json.loads(requests[0].content)
    assert body["stream"] is True and body["thinking_budget"] == 2048
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["max_tokens"] == 4096 + 2048  # the reply cap is never eaten by reasoning


async def test_complete_json_passes_the_think_budget_and_defaults_to_fast():
    llm = FakeLLM('{"answer": "x"}', '{"answer": "y"}')
    await complete_json(llm, MESSAGES, Out)
    await complete_json(llm, MESSAGES, Out, think=True, think_budget=2048)
    assert llm.thinks == [False, True] and llm.budgets == [None, 2048]


@pytest.mark.parametrize("raw, clamped", [(100, 512), (2048, 2048), (10_000, 4096)])
def test_think_escalation_budget_is_clamped(raw, clamped):
    assert Settings(think_escalation_max_tokens=raw).think_escalation_max_tokens == clamped


def test_think_escalation_defaults():
    s = Settings(_env_file=None)
    assert (s.think_escalation_max_tokens, s.think_escalation_score_threshold) == (2048, 0.65)
