"""Template -> IR -> source chunks -> mapping run -> render -> signed download -> open the file."""

import json
import uuid
from io import BytesIO
from urllib.parse import urlsplit

import pytest
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

from doc_intel.core.tenancy import TenantContext
from doc_intel.db.models import Job
from doc_intel.ingestion.schemas import ChunkPayload
from doc_intel.vector.qdrant_store import chunk_id
from tests.conftest import headers
from tests.fakes import FakeEmbedder, FakeLLM
from tests.integration.test_extract_template_job import queue_job
from tests.integration.test_mapping_e2e import batch, candidate, drain_one, extraction
from tests.integration.test_mapping_runs_api import seed_document

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queue", "one_ir_batch")]

CHUNK = "The substation fire on Main Street caused an outage; 12,000 customers lost power."
QUOTE = "substation fire on Main Street caused an outage"
CUSTOMERS_QUOTE = "12,000 customers lost power"

# (format, populated anchor, write_mode, type, candidate value, missing anchor)
CASES = {
    "pptx": ("pptx:s2:sp3", "replace_token", "text", "Substation fire", "pptx:s3:sp5"),
    "docx": ("docx:body:p3", "replace_token", "text", "Substation fire", "docx:sdt:owner"),
    "xlsx": ("xlsx:Summary!B2", "fill_cell", "number", "12,000", "xlsx:Summary!D1"),
}
TOKENS = {"pptx": "{{incident_date}}", "docx": "{{client_name}}"}


def draft_reply(fmt: str) -> str:
    anchor_id, mode, etype, _, missing_anchor = CASES[fmt]
    base = {
        "section_path": ["S"],
        "description": "A field.",
        "mode": "extractive",
        "required": True,
        "constraints": {},
    }
    found_constraints = {"token": TOKENS[fmt]} if fmt in TOKENS else {}
    fields = [
        {**base, "field_id": "found", "anchor_id": anchor_id, "label": "Found field",
         "expected_type": etype, "write_mode": mode, "constraints": found_constraints},
        {**base, "field_id": "lost", "anchor_id": missing_anchor, "label": "Spokesperson",
         "expected_type": "text", "write_mode": "replace_text" if fmt != "xlsx" else "fill_cell"},
    ]  # fmt: skip
    return json.dumps({"fields": fields})


@pytest.mark.parametrize("fmt", ["pptx", "docx", "xlsx"])
async def test_round_trip(client, session, migrated_db, vector_store, user_a, project, fmt):
    ctx = TenantContext(user_a, uuid.UUID(project))
    _, extract_job = await queue_job(client, session, user_a, project, fmt)
    await drain_one(migrated_db, vector_store, FakeLLM(draft_reply(fmt)))
    session.expire_all()
    ir_id = (await session.get(Job, uuid.UUID(extract_job))).result["template_ir_id"]

    source = await seed_document(session, ctx, name="sitrep.txt")
    payload = ChunkPayload(
        document_id=source, filename="sitrep.txt", page_number=None, section_heading=None,
        element_type="text", chunk_index=0, text=CHUNK,
    )  # fmt: skip
    await vector_store.upsert_chunks(ctx, [payload], await FakeEmbedder().embed_documents([CHUNK]))

    posted = client.post(
        f"/v1/projects/{project}/mapping-runs",
        json={"template_ir_id": ir_id, "document_ids": None},
        headers=headers(user_a),
    )
    assert posted.status_code == 202, posted.text
    run_id = posted.json()["mapping_run_id"]

    value = CASES[fmt][3]
    quote = CUSTOMERS_QUOTE if fmt == "xlsx" else QUOTE
    found = [candidate(value, chunk_id(source, 0), quote)]
    # found and lost share a unit (one batch call) except in pptx, where the lone `found` is a
    # batch of one on the opening slide and `lost` is mapped alone on slide 2
    # pptx's `lost` box is sized for prose: its miss gets one synthesis try
    no_synthesis = json.dumps({"items": [], "not_found": True})
    pptx = [batch(found), extraction([], not_found=True), no_synthesis]
    slots = pptx if fmt == "pptx" else [batch(found, [])]
    llm = FakeLLM(
        *slots,
        json.dumps({"rows": [], "not_found": True}),  # pptx/docx: the empty table's code field
        *([extraction([], not_found=True)] if fmt == "docx" else []),  # header `<classification>`
    )
    await drain_one(migrated_db, vector_store, llm)

    run = client.get(
        f"/v1/projects/{project}/mapping-runs/{run_id}", headers=headers(user_a)
    ).json()
    assert run["status"] == "completed", run
    assert run["results"]["found"]["status"] == "populated"
    assert run["results"]["lost"]["status"] == "missing"
    if fmt == "docx":  # a named token the IR left uncovered gets a code-made field
        assert run["results"]["classification"]["status"] == "missing"
    artifact = run["artifact"]
    assert artifact["format"] == fmt
    assert artifact["filename"].endswith(f".{fmt}")

    parts = urlsplit(artifact["download_url"])
    got = client.get(f"{parts.path}?{parts.query}")  # no bearer: the signature is the credential
    assert got.status_code == 200
    assert len(got.content) == artifact["size_bytes"]
    check = {"pptx": check_pptx, "docx": check_docx, "xlsx": check_xlsx}[fmt]
    check(got.content)


def check_pptx(data: bytes) -> None:
    prs = Presentation(BytesIO(data))
    texts = [s.text_frame.text for sl in prs.slides for s in sl.shapes if s.has_text_frame]
    assert "Date Substation fire status TBD" in texts  # only the token is replaced
    assert {"Situation Overview", "Incident", "Stakeholders"} <= set(texts)
    group = next(s for s in prs.slides[2].shapes if s.shape_type == 6)
    assert [s.text_frame.text for s in group.shapes] == ["[MISSING: Spokesperson]"]
    table = next(s for s in prs.slides[2].shapes if s.has_table).table
    assert [c.text for c in table.rows[0].cells] == ["Name", "Role", "Owner"]


def check_docx(data: bytes) -> None:
    doc = Document(BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs]
    assert "Client: Substation fire" in paragraphs  # the label around the token is kept
    assert "Incident Report" in paragraphs and "Signature ____" in paragraphs
    assert doc.tables[0].cell(0, 0).text == "Item"
    assert doc.sections[0].footer.paragraphs[0].text == "Page footer"
    assert "[MISSING: Spokesperson]" in doc.element.xml


def check_xlsx(data: bytes) -> None:
    ws = load_workbook(BytesIO(data))["Summary"]
    assert ws["B2"].value == 12000
    assert ws["D1"].value == "[MISSING: Spokesperson]"
    assert ws["B4"].value == "=SUM(B2:B3)"  # the formula is left alone
    assert ws["A1"].value == "Metric"
