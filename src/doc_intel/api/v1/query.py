import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from doc_intel.chat import qa
from doc_intel.core.tenancy import Tenant

router = APIRouter(prefix="/projects/{project_id}/query")


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)  # keeps the prompt within LLM_NUM_CTX
    document_ids: list[uuid.UUID] | None = None


@router.post("")
async def query(body: QueryIn, ctx: Tenant, request: Request) -> qa.Answer:
    state = request.app.state
    return await qa.answer(ctx, body.question, body.document_ids, state.retriever, state.llm)
