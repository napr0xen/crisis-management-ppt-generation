"""Phase 8 end to end: the real Open WebUI Pipe against the real app over ASGI.

Real Pipe, router, summary, ingestion, mapping, rendering and signed download. Scripted: the LLM
(by system prompt), the embedder (seeded fake) and Open WebUI's file read (`_read_file`).
"""

import importlib.util
import io
import json
import re
from pathlib import Path

import httpx
import pytest
from pptx import Presentation

from doc_intel.llm.prompts import load_prompt
from tests.conftest import PIPE_SECRET
from tests.fixtures import templates
from tests.integration.test_generation_e2e import CHUNK, QUOTE, draft_reply
from tests.integration.test_mapping_e2e import candidate, extraction
from tests.integration.test_pipe_api import fast_poll, live_app  # noqa: F401 (fixture)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("empty_queue", "fast_poll", "one_ir_batch"),
]

ROOT = Path(__file__).resolve().parents[2]
CHUNK_ID = re.compile(r"\[([0-9a-f-]{36}) \|")
PUBLIC = "http://127.0.0.1:8000"
FILES = {"tpl": ("update.pptx", templates.pptx_template()), "src": ("sitrep.txt", CHUNK.encode())}


class ScriptedLLM:
    """Replies by system prompt: the router picks the pptx, extraction and QA cite the first hit."""

    def __init__(self) -> None:
        self.intent = "map_template"
        self.extractions = 0

    async def complete(self, messages, json_schema=None) -> str:
        system, user = messages[0]["content"], messages[-1]["content"]
        if system == load_prompt("router"):
            tpl = next(ln.split(" | ")[0] for ln in user.splitlines() if "update.pptx" in ln)
            return json.dumps({"intent": self.intent, "template_document_id": tpl})
        if system == load_prompt("template_ir"):
            return draft_reply("pptx")
        hit = CHUNK_ID.search(user).group(1)
        if system == load_prompt("qa"):
            citation = {"marker": 1, "chunk_id": hit, "quote": QUOTE}
            return json.dumps({"answer_markdown": "A fire [1].", "citations": [citation]})
        assert system == load_prompt("field_extract")
        self.extractions += 1
        if self.extractions == 1:
            return extraction([candidate("Substation fire", hit, QUOTE)])
        return extraction([], not_found=True)


def make_pipe(app, user):
    spec = importlib.util.spec_from_file_location(
        "doc_intel_pipe_e2e", ROOT / "integrations/openwebui/doc_intel_pipe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pipe = module.Pipe()
    pipe.valves.PIPE_SHARED_SECRET = PIPE_SECRET
    pipe._client = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    )

    async def read_file(file_id, user_id):
        assert user_id == user
        return FILES[file_id][1]

    pipe._read_file = read_file
    return pipe


async def turn(pipe, user, text):
    statuses = []

    async def emitter(event):
        statuses.append(event["data"])

    body = {"messages": [{"role": "user", "content": text}]}
    files = [{"type": "file", "id": fid, "name": name} for fid, (name, _) in FILES.items()]
    gen = pipe.pipe(body, {"id": user}, {"chat_id": "chat-e2e"}, files, emitter)
    return "".join([c async for c in gen]), statuses


async def test_pipe_fills_template_then_answers_with_citations(migrated_db, vector_store, user_a):
    llm = ScriptedLLM()
    async with live_app(migrated_db, vector_store, llm) as (ac, _, app):
        pipe = make_pipe(app, user_a)
        reply, statuses = await turn(pipe, user_a, "fill the template")

        assert statuses[-1] == {"description": "Done", "done": True}, reply
        descriptions = [s["description"] for s in statuses]
        # Job-stage statuses depend on poll timing; uploads are always reported.
        assert {"Uploading update.pptx", "Uploading sitrep.txt"} <= set(descriptions)
        assert "**Template mapping finished:** 1 populated, 0 partial, 1 missing" in reply
        assert "- Spokesperson: not stated in the documents" in reply
        url = re.search(r"\[Download the filled template\]\((\S+)\)", reply).group(1)
        assert url.startswith(f"{PUBLIC}/v1/artifacts/")

        download = await ac.get(url.removeprefix(PUBLIC))
        assert download.status_code == 200
        slide_text = "\n".join(
            shape.text_frame.text
            for slide in Presentation(io.BytesIO(download.content)).slides
            for shape in slide.shapes
            if shape.has_text_frame
        )
        assert "Substation fire" in slide_text

        # Same chat, next turn: files are re-sent (deduplicated) and the answer cites the source.
        llm.intent = "qa"
        answer, _ = await turn(pipe, user_a, "what happened?")
        assert answer.startswith("A fire [1].")
        assert f'[1] sitrep.txt: "{QUOTE}"' in answer

    assert PIPE_SECRET not in reply + answer + json.dumps(statuses)
