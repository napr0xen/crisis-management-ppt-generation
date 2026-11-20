"""Unit tests for the Open WebUI Pipe (backend replaced by httpx.MockTransport)."""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

PIPE_PATH = Path(__file__).parents[2] / "integrations" / "openwebui" / "doc_intel_pipe.py"
SECRET = "s3cr3t-value"
USER = {"id": "user-1"}
META = {"chat_id": "chat-9"}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("doc_intel_pipe", PIPE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c


def sse(*events: tuple[str, dict]) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def sse_response(body: bytes | list[bytes]) -> httpx.Response:
    chunks = [body] if isinstance(body, bytes) else body
    return httpx.Response(200, stream=Chunks(chunks), headers={"content-type": "text/event-stream"})


def envelope(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": message, "request_id": "r"}}
    )


DONE = ("done", {"project_id": "p", "mapping_run_id": None, "artifact_id": None})


class Harness:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.statuses: list[dict] = []
        self.outputs: list[str] = []
        self.pipe = _load().Pipe()
        self.pipe.valves.PIPE_SHARED_SECRET = SECRET

        def wrapped(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return handler(req)

        transport = httpx.MockTransport(wrapped)
        self.pipe._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
            base_url="http://backend", transport=transport
        )

        async def read(file_id: str, user_id: str) -> bytes:
            return b"bytes-of-" + file_id.encode()

        self.pipe._read_file = read  # type: ignore[method-assign]

    async def run(
        self, body: dict, meta: dict | None = None, files: list | None = None, emit: bool = True
    ) -> str:
        async def emitter(ev: dict) -> None:
            self.statuses.append(ev)

        async for piece in self.pipe.pipe(
            body,
            USER,
            META if meta is None else meta,
            files,
            emitter if emit else None,
        ):
            self.outputs.append(piece)
        text = "".join(self.outputs)
        assert SECRET not in text
        assert SECRET not in json.dumps(self.statuses)
        return text

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def msgs(text: str = "hi") -> dict:
    return {"messages": [{"role": "user", "content": text}]}


def ok_chat(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/v1/pipe/files":
        return httpx.Response(201, json={"document_id": "d"})
    return sse_response(sse(("delta", {"content": "ok"}), DONE))


async def test_task_via_metadata_makes_no_call() -> None:
    h = Harness(ok_chat)
    assert await h.run(msgs(), {"chat_id": "c", "task": "title_generation"}) == ""
    assert h.requests == []


async def test_task_prefix_makes_no_call() -> None:
    h = Harness(ok_chat)
    assert await h.run(msgs("### Task:\nGenerate a title")) == ""
    assert h.requests == []


async def test_missing_chat_id_message() -> None:
    h = Harness(ok_chat)
    text = await h.run(msgs(), {})
    assert "chat" in text.lower() and text
    assert h.requests == []


async def test_headers_on_both_endpoints() -> None:
    h = Harness(ok_chat)
    await h.run(msgs(), files=[{"id": "f1", "name": "a.pdf"}])
    assert h.paths() == ["/v1/pipe/files", "/v1/pipe/chat"]
    for r in h.requests:
        assert r.headers["authorization"] == f"Bearer {SECRET}"
        assert r.headers["x-owui-user-id"] == "user-1"
        assert r.headers["x-owui-chat-id"] == "chat-9"


async def test_files_deduped_and_multipart() -> None:
    h = Harness(ok_chat)
    files = [
        {"type": "file", "id": "f1", "name": "a.pdf"},
        {"file": {"id": "f2", "filename": "b.docx"}},
        {"id": "f1", "name": "a.pdf"},
        {"type": "collection", "id": "c1", "name": "kb"},
    ]
    await h.run(msgs(), files=files)
    uploads = [r for r in h.requests if r.url.path == "/v1/pipe/files"]
    assert len(uploads) == 2
    body = uploads[0].content
    assert b'name="openwebui_file_id"' in body and b"f1" in body
    assert b"bytes-of-f1" in body and b'filename="a.pdf"' in body
    assert b"bytes-of-f2" in uploads[1].content and b'filename="b.docx"' in uploads[1].content
    chat = json.loads(h.requests[-1].content)
    assert chat["files"] == [
        {"openwebui_file_id": "f1", "filename": "a.pdf"},
        {"openwebui_file_id": "f2", "filename": "b.docx"},
    ]
    assert {"Uploading a.pdf", "Uploading b.docx"} <= {s["data"]["description"] for s in h.statuses}


async def test_upload_failure_message_escapes_the_filename() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/pipe/files":
            return envelope(415, "unsupported_media_type", "Unsupported file type.")
        return ok_chat(req)

    h = Harness(handler)
    text = await h.run(msgs(), files=[{"id": "f1", "name": "![t](https://tracker.example/p.png)"}])
    assert "![t](" not in text and "\\!\\[t\\]\\(https:\\/\\/tracker.example/p.png\\)" in text


async def test_upload_failure_message_and_exclusion() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/pipe/files":
            return envelope(415, "unsupported_media_type", "Unsupported file type.")
        return ok_chat(req)

    h = Harness(handler)
    text = await h.run(msgs(), files=[{"id": "f1", "name": "a.exe"}])
    assert "Could not upload a.exe: Unsupported file type." in text
    assert json.loads(h.requests[-1].content)["files"] == []


async def test_upload_read_failure_is_generic_and_hides_details() -> None:
    h = Harness(ok_chat)

    async def boom(file_id: str, user_id: str) -> bytes:
        raise OSError("/app/backend/data/uploads/secret-path")

    h.pipe._read_file = boom  # type: ignore[method-assign]
    text = await h.run(msgs(), files=[{"id": "f1", "name": "a.pdf"}])
    assert "Could not upload a.pdf" in text
    assert "/app/backend" not in text
    assert json.loads(h.requests[-1].content)["files"] == []


async def test_upload_transport_error_does_not_abort() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/pipe/files":
            raise httpx.ConnectError("nope")
        return ok_chat(req)

    h = Harness(handler)
    text = await h.run(msgs(), files=[{"id": "f1", "name": "a.pdf"}])
    assert "Could not upload a.pdf" in text and "ok" in text


async def test_sse_mapping_in_order() -> None:
    body = sse(
        ("status", {"description": "one", "done": False}),
        ("status", {"description": "two", "done": False}),
        ("delta", {"content": "Hello "}),
        ("delta", {"content": "world"}),
        DONE,
    )
    h = Harness(lambda r: sse_response(body))
    text = await h.run(msgs())
    assert text == "Hello world"
    assert [s["data"]["description"] for s in h.statuses] == ["one", "two", "Done"]
    assert all(s["type"] == "status" for s in h.statuses)
    assert h.statuses[-1]["data"]["done"] is True


async def test_sse_split_across_chunks_and_comments() -> None:
    raw = b": keepalive\n\n" + sse(("delta", {"content": "abc"}), DONE)
    chunks = [raw[i : i + 5] for i in range(0, len(raw), 5)]
    h = Harness(lambda r: sse_response(chunks))
    assert await h.run(msgs()) == "abc"
    assert h.statuses[-1]["data"]["description"] == "Done"


async def test_stops_after_done() -> None:
    body = sse(DONE, ("delta", {"content": "late"}))
    h = Harness(lambda r: sse_response(body))
    assert await h.run(msgs()) == ""


async def test_error_event() -> None:
    body = sse(("error", {"code": "internal", "message": "Boom happened"}))
    h = Harness(lambda r: sse_response(body))
    text = await h.run(msgs())
    assert "Boom happened" in text
    assert h.statuses[-1]["data"] == {"description": "Boom happened", "done": True}


async def test_no_emitter_works() -> None:
    body = sse(("status", {"description": "x", "done": False}), ("delta", {"content": "y"}), DONE)
    h = Harness(lambda r: sse_response(body))
    assert await h.run(msgs(), emit=False) == "y"
    assert h.statuses == []


async def test_chat_401_hint() -> None:
    h = Harness(lambda r: envelope(401, "unauthorized", "Unauthorized."))
    text = await h.run(msgs())
    assert "PIPE_SHARED_SECRET" in text


async def test_chat_503_envelope_message() -> None:
    h = Harness(lambda r: envelope(503, "upstream_unavailable", "Qdrant is down."))
    assert "Qdrant is down." in await h.run(msgs())


async def test_chat_non_json_body() -> None:
    h = Harness(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    text = await h.run(msgs())
    assert "Request failed (HTTP 502)." in text
    assert "bad gateway" not in text


async def test_connect_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    h = Harness(handler)
    assert "The document service is unreachable." in await h.run(msgs())


async def test_timeout() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    h = Harness(handler)
    assert "The document service timed out." in await h.run(msgs())


async def test_other_http_error_generic() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("bad")

    h = Harness(handler)
    text = await h.run(msgs())
    assert text and "bad" not in text


async def test_messages_flattened_filtered_and_capped() -> None:
    parts = [{"type": "text", "text": "a"}, {"type": "image_url"}, {"type": "text", "text": "b"}]
    many = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    body = {"messages": [{"role": "system", "content": "sys"}, *many[:-1]]}
    body["messages"].append({"role": "user", "content": parts})
    h = Harness(ok_chat)
    await h.run(body)
    sent = json.loads(h.requests[-1].content)["messages"]
    assert len(sent) == 20
    assert sent[-1] == {"role": "user", "content": "a\nb"}
    assert all(m["role"] in ("user", "assistant") for m in sent)
    assert "sys" not in json.dumps(sent)


async def test_valves_defaults_and_real_client_config() -> None:
    mod = _load()
    p = mod.Pipe()
    assert p.valves.BACKEND_URL == "http://host.docker.internal:8000"
    assert p.valves.TIMEOUT_S == 1800
    assert p.valves.PIPE_SHARED_SECRET == ""
    client = p._client()
    assert str(client.base_url).startswith("http://host.docker.internal:8000")
    assert client.timeout.read == 1800 and client.timeout.connect == 10
    await client.aclose()


async def test_read_file_uses_open_webui_storage(monkeypatch, tmp_path) -> None:
    import sys
    import threading
    import types

    f = tmp_path / "x.bin"
    f.write_bytes(b"payload")

    class Files:
        @staticmethod
        async def get_file_by_id_and_user_id(fid: str, uid: str):
            return types.SimpleNamespace(path="p") if fid == "ok" else None

    threads: list[int] = []

    class Storage:
        @staticmethod
        def get_file(path: str) -> str:
            threads.append(threading.get_ident())
            return str(f)

    models = types.ModuleType("open_webui.models.files")
    models.Files = Files  # type: ignore[attr-defined]
    prov = types.ModuleType("open_webui.storage.provider")
    prov.Storage = Storage  # type: ignore[attr-defined]
    for name, m in {
        "open_webui": types.ModuleType("open_webui"),
        "open_webui.models": types.ModuleType("open_webui.models"),
        "open_webui.models.files": models,
        "open_webui.storage": types.ModuleType("open_webui.storage"),
        "open_webui.storage.provider": prov,
    }.items():
        monkeypatch.setitem(sys.modules, name, m)

    p = _load().Pipe()
    assert await p._read_file("ok", "u") == b"payload"
    with pytest.raises(FileNotFoundError):
        await p._read_file("missing", "u")
    assert threads and threads[0] != threading.get_ident()


async def test_malformed_files_entries_are_skipped() -> None:
    h = Harness(ok_chat)
    files = [{}, {"file": {}}, {"type": "file", "name": "x"}, {"id": "f1"}, "junk"]
    text = await h.run(msgs(), files=files)
    assert text == "ok"
    assert h.paths() == ["/v1/pipe/chat"]


async def test_non_dict_content_parts_ignored() -> None:
    h = Harness(ok_chat)
    body = msgs()
    body["messages"][0]["content"] = ["junk", 3, None, {"type": "text", "text": "a"}]
    await h.run(body)
    assert json.loads(h.requests[-1].content)["messages"][0]["content"] == "a"


@pytest.mark.parametrize(
    "raw",
    [
        b"event: delta\ndata: {not json\n\n",
        b"event: delta\ndata: {}\n\n",
        b"event: status\ndata: []\n\n",
    ],
)
async def test_malformed_frame_ends_gracefully(raw: bytes) -> None:
    h = Harness(lambda r: sse_response(raw + sse(("delta", {"content": "late"}), DONE)))
    text = await h.run(msgs())
    assert text == "The document service sent an invalid response."
    assert h.statuses[-1]["data"]["done"] is True


async def test_stream_closed_without_done() -> None:
    h = Harness(lambda r: sse_response(sse(("delta", {"content": "part"}))))
    text = await h.run(msgs())
    assert text == "partThe document service closed the connection early."
    assert h.statuses[-1]["data"]["done"] is True


async def test_keepalive_comments_interleaved() -> None:
    raw = (
        b": keep-alive\n\n"
        + sse(("delta", {"content": "a"}))
        + b": keep-alive\n"
        + b": keep-alive\n\n"
        + sse(("delta", {"content": "b"}), DONE)
    )
    h = Harness(lambda r: sse_response(raw))
    assert await h.run(msgs()) == "ab"
