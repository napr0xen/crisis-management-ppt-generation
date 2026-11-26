"""Sentinel strings never reach a log (testing-strategy § 4, guardrail 6).

A unique sentinel sits in document text and names, template labels and tokens, chat messages,
LLM completions, edit values and Open WebUI file names. Every job handler and chat path runs, plus
the error paths; structlog events (after the app's redaction processor) and every stdlib record,
at DEBUG, are searched for it, and so are stdout/stderr.
"""

import asyncio
import json
import logging
import uuid
import zipfile
from io import BytesIO

import pytest
import structlog
from pptx import Presentation
from pptx.util import Inches
from sqlalchemy import delete, update

from doc_intel.core.logging import redact_forbidden
from doc_intel.db.models import Document, Job
from doc_intel.llm.gateway import LLMError
from doc_intel.templates.anchors import build_anchor_map
from doc_intel.vector.qdrant_store import chunk_id
from tests.conftest import headers
from tests.integration.test_mapping_e2e import candidate, extraction
from tests.integration.test_pipe_api import (  # noqa: F401 (fixtures)
    api_chat,
    api_file,
    events,
    fast_poll,
    live_app,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("empty_queue", "fast_poll", "one_ir_batch"),
]

CORE = "9f3a7c1e5b2d"
S = f"SENTINEL_{CORE}"
TOKEN = "{{" + S + "_tok}}"
SOURCE = (
    f"Briefing {S}. The substation fire on Main Street caused an outage. "
    "12,000 customers lost power. Later 15,000 customers lost power."
)
FIELDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


class Logs:
    """Everything logged during a test, as text."""

    def __init__(self, caplog, capsys) -> None:
        self.structlog: list[str] = []
        self._caplog, self._capsys = caplog, capsys

    def text(self) -> str:
        std = [repr(r.__dict__) for r in self._caplog.records] + [self._caplog.text]
        out, err = self._capsys.readouterr()
        return "\n".join([*self.structlog, *std, out, err])

    def assert_clean(self) -> None:
        text = self.text()
        leaked = [line for line in text.splitlines() if CORE in line.lower()]
        assert not leaked, leaked[0][:300]


@pytest.fixture
def logs(monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)  # root and its handler: uvicorn, sqlalchemy, httpx, asyncio
    for name in ("httpx", "httpcore", "uvicorn", "asyncio"):
        caplog.set_level(logging.DEBUG, logger=name)
    # INFO logs every statement with hide_parameters=True (make_engine); only an operator forcing
    # the engine logger to DEBUG makes SQLAlchemy print result rows, which nothing in the app does.
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    captured = Logs(caplog, capsys)

    def capture(_logger, _method, event_dict: dict) -> dict:
        captured.structlog.append(repr(event_dict))  # repr: exc_info objects and nested values
        return event_dict

    def configure(_level: str) -> None:  # the app's chain, all levels, kept in memory
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                redact_forbidden,
                capture,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            logger_factory=structlog.ReturnLoggerFactory(),
            cache_logger_on_first_use=False,
        )

    monkeypatch.setattr("doc_intel.main.configure_logging", configure)
    return captured


class ScriptedLLM:
    """`LLMClient` answering by the requested schema's name: a queue of replies or a function of
    the messages. A reply that is an exception is raised."""

    def __init__(self, **handlers) -> None:
        self.handlers = handlers

    async def complete(self, messages, json_schema=None, think=False, think_budget=None) -> str:
        handler = self.handlers[json_schema["title"].split("[")[0]]
        reply = handler(messages) if callable(handler) else handler.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def sentinel_pptx() -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    texts = [f"Date {TOKEN} x", f"Owner {S}", "Cause", "Customers", "Contact", "Phone"]
    for i, text in enumerate(texts):
        box = slide.shapes.add_textbox(Inches(1), Inches(1 + i), Inches(4), Inches(1))
        box.text_frame.text = text
    out = BytesIO()
    prs.save(out)
    return out.getvalue()


def ir_reply() -> str:
    anchors = build_anchor_map("pptx", sentinel_pptx()).anchors
    fields = []
    for fid, anchor in zip(FIELDS, anchors, strict=True):
        fields.append(
            {
                "field_id": fid,
                "anchor_id": anchor.anchor_id,
                "section_path": ["Slide 1", S],
                "label": f"{fid.title()} {S}",
                "description": f"A field about {S}.",
                "expected_type": "text",
                "mode": "extractive",
                "required": True,
                "write_mode": "replace_token" if fid == "alpha" else "replace_text",
                "constraints": {"token": TOKEN} if fid == "alpha" else {},
            }
        )
    return json.dumps({"fields": fields})


def extraction_reply(source_id: uuid.UUID, fault: str | None = None):
    chunk = chunk_id(source_id, 0)

    def reply(messages) -> str:
        spec = messages[1]["content"]
        if "Alpha" in spec:
            return extraction(
                [
                    candidate(
                        "Substation fire", chunk, "substation fire on Main Street caused an outage"
                    )
                ]
            )
        if "Bravo" in spec:  # the quote is not in the source: verification fails
            return extraction([candidate(f"Ghost {S}", chunk, f"never written {S} anywhere")])
        if "Charlie" in spec:  # two verified, different values: a conflict
            return extraction(
                [
                    candidate("12,000", chunk, "12,000 customers lost power"),
                    candidate("15,000", chunk, "15,000 customers lost power"),
                ]
            )
        if "Echo" in spec:
            return f"not json {S}"  # invalid twice: LLMInvalidOutput
        if "Foxtrot" in spec and fault == "extract":
            raise RuntimeError(f"boom {S}")
        if "Foxtrot" in spec and fault == "upstream":
            raise LLMError(f"down {S}")  # a transport failure fails the job
        return extraction([], not_found=True)

    return reply


async def map_template(ac, app, user, fault=None) -> tuple[list, str, uuid.UUID]:
    """Upload a template and a source with sentinel names, then map through the chat."""
    tpl = await api_file(ac, user, "tpl", f"tpl_{S}.pptx", sentinel_pptx())
    src = await api_file(ac, user, "src", f"src_{S}.txt", SOURCE.encode())
    source_id = uuid.UUID(src["document_id"])
    app.state.llm.handlers.update(
        _LLMIntent=lambda m: json.dumps(
            {"intent": "map_template", "template_document_id": tpl["document_id"]}
        ),
        BatchReply=lambda m: ir_reply(),
        # one slide holds every field: an invalid batch reply sends each field to its own call
        LLMSlotValues=lambda m: f"not json {S}",
        LLMExtraction=extraction_reply(source_id, fault),
    )
    files = [("tpl", f"tpl_{S}.pptx"), ("src", f"src_{S}.txt")]
    frames = await api_chat(ac, user, files, content=f"fill {S} from {S}")
    return frames, tpl["project_id"], source_id


async def wait_run(ac, user, project, run_id) -> dict:
    for _ in range(300):
        r = await ac.get(f"/v1/projects/{project}/mapping-runs/{run_id}", headers=headers(user))
        if r.json()["status"] in ("completed", "failed"):
            return r.json()
        await asyncio.sleep(0.05)
    raise AssertionError("run did not finish")


async def test_every_handler_and_chat_path_logs_no_sentinel(
    migrated_db, vector_store, user_a, logs
):
    llm = ScriptedLLM(
        _LLMIntent=[],
        BatchReply=[],
        LLMExtraction=[],
        LLMCandidateCheck=lambda _: '{"checks": [{"about": "x", "match": true}]}',
        _LLMAnswer=[],
        _LLMEdits=[],
    )
    async with live_app(migrated_db, vector_store, llm) as (ac, _, app):
        # ingest_document x2, extract_template, run_mapping (verification failure, conflict,
        # invalid JSON), render_artifact
        frames, project, source_id = await map_template(ac, app, user_a)
        assert "error" not in events(frames), frames
        run_id = frames[-1][1]["mapping_run_id"]
        run = await wait_run(ac, user_a, project, run_id)
        statuses = {fid: r["status"] for fid, r in run["results"].items()}
        assert statuses["alpha"] == "populated" and statuses["charlie"] == "conflict"
        assert statuses["bravo"] != "populated" and statuses["delta"] == "missing"

        # POST /v1/projects/{p}/mapping-runs/{run}/edits, then the render job it queues
        posted = await ac.post(
            f"/v1/projects/{project}/mapping-runs/{run_id}/edits",
            json={
                "edits": [
                    {"op": "set_value", "field_id": "bravo", "value": f"Edit {S}"},
                    {"op": "choose_candidate", "field_id": "charlie", "candidate_index": 0},
                ]
            },
            headers=headers(user_a),
        )
        assert posted.status_code == 202, posted.text
        assert (await wait_run(ac, user_a, project, posted.json()["mapping_run_id"]))["artifact"]

        # pipe chat: edit (applied, then rejected as not written by the user), qa, chitchat
        h = llm.handlers
        h["_LLMIntent"] = lambda m: json.dumps({"intent": "edit"})
        h["_LLMEdits"] = lambda m: json.dumps(
            {"edits": [{"op": "set_value", "field_id": "bravo", "value": f"Chat {S}"}]}
        )
        frames = await api_chat(ac, user_a, [], content=f"set bravo to Chat {S}")
        assert "error" not in events(frames) and frames[-1][1]["mapping_run_id"], frames
        await wait_run(ac, user_a, project, frames[-1][1]["mapping_run_id"])
        h["_LLMEdits"] = lambda m: json.dumps(
            {"edits": [{"op": "set_value", "field_id": "bravo", "value": f"Mallory {S}"}]}
        )
        frames = await api_chat(ac, user_a, [], content=f"change bravo {S}")
        assert any("could not apply" in d["content"] for e, d in frames if e == "delta"), frames

        cited = str(chunk_id(source_id, 0))
        answer = {
            "answer_markdown": f"About 12,000 [1] {S}",
            "citations": [
                {"marker": 1, "chunk_id": cited, "quote": "12,000 customers lost power"},
                {"marker": 2, "chunk_id": cited, "quote": f"fake {S}"},
            ],
        }
        h["_LLMIntent"] = lambda m: json.dumps({"intent": "qa"})
        h["_LLMAnswer"] = lambda m: json.dumps(answer)
        frames = await api_chat(ac, user_a, [], content=f"how many customers {S}?")
        assert "error" not in events(frames), frames
        h["_LLMIntent"] = lambda m: json.dumps({"intent": "chitchat"})
        frames = await api_chat(ac, user_a, [], content=f"hello {S}")
        assert "error" not in events(frames), frames

        # POST /v1/projects/{p}/query, grounded and with an invalid-JSON reply
        body = {"question": f"how many customers {S}?", "document_ids": None}
        r = await ac.post(f"/v1/projects/{project}/query", json=body, headers=headers(user_a))
        assert r.status_code == 200, r.text
        h["_LLMAnswer"] = lambda m: f"not json {S}"
        r = await ac.post(f"/v1/projects/{project}/query", json=body, headers=headers(user_a))
        assert r.status_code == 503, r.text
    assert any("job_started" in e for e in logs.structlog), "structlog capture is not wired"
    logs.assert_clean()


@pytest.mark.parametrize(
    "exc",
    [LLMError(f"upstream {S}"), RuntimeError(f"boom {S}"), None],
    ids=["upstream", "unexpected", "invalid_json"],
)
async def test_chat_error_paths_log_no_sentinel(migrated_db, vector_store, user_a, logs, exc):
    def route(messages):
        if exc is None:
            return f"not json {S}"
        raise exc

    async with live_app(migrated_db, vector_store, ScriptedLLM(_LLMIntent=route)) as (ac, _, _):
        frames = await api_chat(ac, user_a, [("nope", f"missing_{S}.pdf")], content=f"hi {S}")
    assert events(frames)[-2:] == ["error", "done"]
    logs.assert_clean()


async def test_failed_ingest_invalid_ir_and_unreadable_template_log_no_sentinel(
    migrated_db, vector_store, user_a, logs
):
    bad_ir = json.loads(ir_reply())
    bad_ir["fields"][0]["anchor_id"] = f"pptx:{S}"  # unknown anchor: free text in the id
    broken = BytesIO()
    with zipfile.ZipFile(BytesIO(sentinel_pptx())) as src, zipfile.ZipFile(broken, "w") as z:
        for item in src.infolist():  # a real pptx whose first slide is not XML
            z.writestr(
                item,
                f"<not-xml {S}" if item.filename == "ppt/slides/slide1.xml" else src.read(item),
            )

    llm = ScriptedLLM(_LLMIntent=[], BatchReply=lambda m: json.dumps(bad_ir))
    async with live_app(migrated_db, vector_store, llm, start=False) as (ac, sm, app):
        await api_file(ac, user_a, "pdf", f"bad_{S}.pdf", b"%PDF-1.7 " + S.encode())
        tpl = await api_file(ac, user_a, "tpl", f"tpl_{S}.pptx", sentinel_pptx())
        bad = await api_file(ac, user_a, "bad", f"broken_{S}.pptx", broken.getvalue())
        template_ids = [uuid.UUID(tpl["document_id"]), uuid.UUID(bad["document_id"])]
        async with sm.begin() as s:  # templates are mapped from indexed documents
            await s.execute(
                delete(Job).where(Job.id.in_([uuid.UUID(tpl["job_id"]), uuid.UUID(bad["job_id"])]))
            )
            await s.execute(
                update(Document).where(Document.id.in_(template_ids)).values(status="indexed")
            )
        app.state.go.set()
        names = [f"tpl_{S}.pptx", f"broken_{S}.pptx"]  # two templates: the user names one
        for doc_id, name in zip(template_ids, names, strict=True):  # invalid IR, then unreadable
            llm.handlers["_LLMIntent"] = lambda m, d=doc_id: json.dumps(
                {"intent": "map_template", "template_document_id": str(d)}
            )
            frames = await api_chat(ac, user_a, [("pdf", f"bad_{S}.pdf")], content=f"map {name}")
            assert events(frames)[-2:] == ["error", "done"], frames
    logs.assert_clean()


@pytest.mark.parametrize("fault", ["render_key", "render_value", "extract", "upstream"])
async def test_render_and_mapping_failures_log_no_sentinel(
    migrated_db, vector_store, user_a, logs, monkeypatch, fault
):
    def render(*args, **kwargs):
        raise KeyError(S) if fault == "render_key" else ValueError(S)

    if fault.startswith("render"):
        monkeypatch.setattr("doc_intel.writers.render", render)
    llm = ScriptedLLM(_LLMIntent=[], BatchReply=[], LLMExtraction=[])
    async with live_app(migrated_db, vector_store, llm) as (ac, _, app):
        frames, _, _ = await map_template(ac, app, user_a, fault)
        assert events(frames)[-2:] == ["error", "done"], frames
    logs.assert_clean()


@pytest.mark.parametrize("sink", ["structlog", "stdlib", "stdout"])
def test_the_harness_catches_a_leak(client, logs, sink, capsys):
    if sink == "structlog":
        structlog.get_logger().info("probe", detail=S)
    elif sink == "stdlib":
        logging.getLogger("probe").warning("probe %s", S)
    else:
        print(S)
    with pytest.raises(AssertionError):
        logs.assert_clean()
