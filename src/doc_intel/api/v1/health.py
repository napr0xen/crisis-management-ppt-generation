from importlib.metadata import version

from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter()


async def _postgres(request: Request) -> str:
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "unavailable"


async def _qdrant(request: Request) -> str:
    # Plain readiness probe; data access goes through vector/qdrant_store.py from Phase 3.
    try:
        url = f"{request.app.state.settings.qdrant_url}/readyz"
        r = await request.app.state.http.get(url, timeout=2.0)
        return "ok" if r.status_code == 200 else "unavailable"
    except Exception:
        return "unavailable"


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    deps = {"postgres": await _postgres(request), "qdrant": await _qdrant(request)}
    status = "ok" if all(v == "ok" for v in deps.values()) else "degraded"
    return {"status": status, **deps, "version": version("doc-intel")}
