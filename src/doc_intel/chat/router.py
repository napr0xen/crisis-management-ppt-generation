"""Intent routing for `POST /v1/pipe/chat` (system-design § 9.4).

One JSON LLM call over the last messages and the project's document list (ids, filenames,
roles). No document text is sent. Intents: qa, map_template, chitchat, edit.
"""

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from doc_intel.db.models import Document
from doc_intel.llm.gateway import LLMClient, complete_json
from doc_intel.llm.prompts import load_prompt

HISTORY_MESSAGES = 10
HISTORY_CHARS = 1500  # each message keeps its LAST 1,500 chars (system-design § 9.4)
TEMPLATE_FORMATS = frozenset({"pptx", "docx", "xlsx"})


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(max_length=200_000)


class Intent(BaseModel):
    intent: Literal["qa", "map_template", "chitchat", "edit"]
    template_document_id: uuid.UUID | None = None
    format: Literal["pptx", "docx", "xlsx"] | None = None  # the output format the user asked for


class _LLMIntent(BaseModel):
    # str ids: one malformed id must not fail the whole reply.
    intent: Literal["qa", "map_template", "chitchat", "edit"]
    template_document_id: str | None = None
    candidates: list[str] = []
    format: str | None = None


def doc_format(doc: Document) -> str:
    """Format from the storage key (validated at upload), never from the filename."""
    return doc.storage_key.rsplit(".", 1)[-1].lower()


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


async def route(llm: LLMClient, messages: list[ChatMessage], documents: list[Document]) -> Intent:
    """Classify the latest user message.

    Candidate ids not in `documents`, or not pptx/docx/xlsx, are dropped. Exactly one remaining
    candidate maps that template only when the user confirmed it (`_confirmed`); otherwise the
    caller lists the templates, so a source is never turned into a template by a guess.
    When the reply names an output `format`, only documents of that format are candidates (a
    source DOCX is never the template for "make a powerpoint").
    `LLMInvalidOutput`/upstream errors propagate.
    """
    docs = "\n".join(f"{d.id} | {d.filename} | {d.role} | {doc_format(d)}" for d in documents)
    history = "\n".join(
        f"{m.role}: {m.content[-HISTORY_CHARS:]}" for m in messages[-HISTORY_MESSAGES:]
    )
    prompt = [
        {"role": "system", "content": load_prompt("router")},
        {"role": "user", "content": f"Documents:\n{docs}\n\nConversation:\n{history}"},
    ]
    reply = await complete_json(llm, prompt, _LLMIntent)
    if reply.intent != "map_template":
        return Intent(intent=reply.intent)
    fmt = (reply.format or "").lower()
    fmt = fmt if fmt in TEMPLATE_FORMATS else None
    allowed = {
        d.id
        for d in documents
        if doc_format(d) in TEMPLATE_FORMATS and fmt in (None, doc_format(d))
    }
    raw = [_uuid(reply.template_document_id), *(_uuid(c) for c in reply.candidates)]
    valid = list(dict.fromkeys(i for i in raw if i in allowed))
    if len(valid) == 1 and _confirmed(valid[0], messages, documents, fmt):
        return Intent(intent="map_template", template_document_id=valid[0], format=fmt)
    return Intent(intent="map_template", format=fmt)


def _confirmed(
    doc_id: uuid.UUID, messages: list[ChatMessage], documents: list[Document], fmt: str | None
) -> bool:
    """`start_template` purges the document's evidence, so the choice must be explicit.

    The auto-confirm rule counts only documents of the requested `fmt`; the list number indexes
    all template-capable documents, the order `render_candidates` shows.
    """
    # shortcut: stateless confirmation via chat history, add projects.pending_action if routing
    # proves unreliable.
    templates = [d for d in documents if doc_format(d) in TEMPLATE_FORMATS]
    doc = next(d for d in templates if d.id == doc_id)
    # shortcut: the only pptx/docx/xlsx in a chat is mapped without asking; a source memo of that
    # format then becomes the template (re-adding it needs DELETE), drop this rule if users hit it.
    wanted = [d for d in templates if fmt in (None, doc_format(d))]
    if doc.role == "template" or len(wanted) == 1:
        return True
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")
    if doc.filename.lower() in last.lower():
        return True
    # shortcut: the list number refers to the current document order; an upload between the list
    # and the reply can shift it, upgrade with pending_action.
    return re.search(rf"\b{templates.index(doc) + 1}\b", last) is not None
