import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from doc_intel.chat import qa
from doc_intel.core.security import UserId
from doc_intel.core.tenancy import require_project

router = APIRouter(prefix="/projects/{project_id}/query")


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)  # keeps the prompt within LLM_NUM_CTX
    document_ids: list[uuid.UUID] | None = None


@router.post("")
async def query(
    project_id: uuid.UUID, body: QueryIn, user_id: UserId, request: Request
) -> qa.Answer:
    state = request.app.state
    # Own short session, not `Tenant`: the LLM call below can take minutes and must not hold a
    # pooled connection (and an open transaction) while it runs.
    async with state.sessionmaker() as session:
        ctx = await require_project(project_id, user_id, session)
    return await qa.answer(ctx, body.question, body.document_ids, state.retriever, state.llm)
