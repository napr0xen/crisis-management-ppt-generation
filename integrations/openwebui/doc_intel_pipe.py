"""
title: Document Intelligence Pipe
description: Thin forwarder from Open WebUI to the Document Intelligence backend.

Install: paste this file into Open WebUI Admin > Functions, enable it, and set the valves
BACKEND_URL, PIPE_SHARED_SECRET and TIMEOUT_S. Then open the Pipe model's capabilities and set
file_context=false and builtin_tools=false, so Open WebUI does not inject file text itself.
The Pipe holds no state and no business logic: it uploads files and relays chat as SSE.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator

import httpx
from pydantic import BaseModel, Field

MAX_MESSAGES = 20
_MD_META = re.compile(r"([\\`*_\[\]()!<>#|~])")


def _md(text: str) -> str:
    """Escape markdown in a filename: Open WebUI renders the reply, so `![x](url)` would load.
    Same rules as the backend's `md()`: one line, no fence, no bare-URL autolink."""
    return _MD_META.sub(r"\\\1", " ".join(text.split())).replace("://", r":\/\/")


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else []
    return "\n".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
    )


def _error_message(resp: httpx.Response) -> str:
    if resp.status_code == 401:
        return (
            "The document service rejected the credentials. "
            "Ask an admin to check the PIPE_SHARED_SECRET valve."
        )
    try:
        return str(resp.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return f"Request failed (HTTP {resp.status_code})."


def _transport_message(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "The document service is unreachable."
    if isinstance(exc, httpx.TimeoutException):
        return "The document service timed out."
    return "The document service request failed."


class Pipe:
    class Valves(BaseModel):
        BACKEND_URL: str = "http://host.docker.internal:8000"
        PIPE_SHARED_SECRET: str = Field(
            default="", json_schema_extra={"input": {"type": "password"}}
        )
        TIMEOUT_S: int = 1800

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.valves.BACKEND_URL,
            timeout=httpx.Timeout(self.valves.TIMEOUT_S, connect=10),
        )

    async def _read_file(self, file_id: str, user_id: str) -> bytes:
        from open_webui.models.files import Files
        from open_webui.storage.provider import Storage

        rec = await Files.get_file_by_id_and_user_id(file_id, user_id)
        if rec is None:
            raise FileNotFoundError(file_id)

        def read() -> bytes:  # Storage.get_file may download (S3/GCS): keep it off the loop
            with open(Storage.get_file(rec.path), "rb") as fh:
                return fh.read()

        return await asyncio.to_thread(read)

    async def _upload(
        self, client: httpx.AsyncClient, file_id: str, name: str, user_id: str
    ) -> str | None:
        """Upload one file; return None on success or a user-facing failure line."""
        label = _md(name)
        try:
            data = await self._read_file(file_id, user_id)
            resp = await client.post(
                "/v1/pipe/files", files={"file": (name, data)}, data={"openwebui_file_id": file_id}
            )
        except httpx.HTTPError as exc:
            return f"Could not upload {label}: {_transport_message(exc)}"
        except Exception:
            return f"Could not upload {label}: the file could not be read."
        if resp.status_code != 201:
            return f"Could not upload {label}: {_error_message(resp)}"
        return None

    async def pipe(
        self,
        body: dict,
        __user__: dict,
        __metadata__: dict,
        __files__: list | None = None,
        __event_emitter__=None,
    ) -> AsyncIterator[str]:
        messages = [
            {"role": m["role"], "content": _text(m.get("content"))}
            for m in body.get("messages", [])
            if m.get("role") in ("user", "assistant")
        ][-MAX_MESSAGES:]
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if __metadata__.get("task") or last_user.startswith("### Task:"):
            return
        chat_id = __metadata__.get("chat_id")
        if not chat_id:
            yield "Use this model from an Open WebUI chat."
            return

        async def emit(description: str, done: bool) -> None:
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": description, "done": done}}
                )

        user_id = __user__["id"]
        headers = {
            "Authorization": f"Bearer {self.valves.PIPE_SHARED_SECRET}",
            "X-OWUI-User-Id": user_id,
            "X-OWUI-Chat-Id": chat_id,
        }
        entries: dict[str, str] = {}
        for e in __files__ or []:
            if isinstance(e, dict) and e.get("type") in (None, "file"):
                f = e.get("file") if isinstance(e.get("file"), dict) else {}
                fid, name = e.get("id") or f.get("id"), e.get("name") or f.get("filename")
                if fid and name:
                    entries.setdefault(fid, name)
        sent: list[dict] = []
        try:
            async with self._client() as client:
                client.headers.update(headers)
                # shortcut: every chat turn re-uploads all files of the branch (Open WebUI
                # re-sends __files__); the backend dedups by sha256. Upgrade by skipping ids
                # the backend already knows if large files make turns slow.
                for fid, name in entries.items():
                    await emit(f"Uploading {name}", False)
                    failure = await self._upload(client, fid, name, user_id)
                    if failure:
                        yield failure + "\n\n"
                    else:
                        sent.append({"openwebui_file_id": fid, "filename": name})
                payload = {"messages": messages, "files": sent}
                async with client.stream("POST", "/v1/pipe/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        message = _error_message(resp)
                        await emit(message, True)
                        yield message
                        return
                    event, data = "message", []
                    async for line in resp.aiter_lines():
                        if line.startswith(":"):
                            continue
                        if line:
                            key, _, value = line.partition(":")
                            if key == "event":
                                event = value.strip()
                            elif key == "data":
                                data.append(value.strip())
                            continue
                        try:
                            payload_in = json.loads("\n".join(data)) if data else {}
                            kind, event, data = event, "message", []
                            if kind == "status":
                                await emit(payload_in["description"], payload_in["done"])
                            elif kind == "delta":
                                yield payload_in["content"]
                            elif kind == "error":
                                await emit(payload_in["message"], True)
                                yield f"\n\n**Error:** {payload_in['message']}"
                                return
                            elif kind == "done":
                                await emit("Done", True)
                                return
                        except (ValueError, KeyError, TypeError):
                            message = "The document service sent an invalid response."
                            await emit(message, True)
                            yield message
                            return
                    message = "The document service closed the connection early."
                    await emit(message, True)
                    yield message
        except httpx.HTTPError as exc:
            message = _transport_message(exc)
            await emit(message, True)
            yield message
