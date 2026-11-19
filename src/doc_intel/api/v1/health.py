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


async def _llm(request: Request) -> str:
    """Reachability of the active provider (no generation, so no model load)."""
    s = request.app.state.settings
    base = s.llm_base_url.rstrip("/")
    key = s.llm_api_key.get_secret_value()
    url, headers = (
        (f"{base}/api/version", None)
        if s.llm_provider == "ollama"
        else (f"{base}/models", {"Authorization": f"Bearer {key}"} if key else None)
    )
    try:
        r = await request.app.state.http.get(url, headers=headers, timeout=2.0)
        return "ok" if r.status_code == 200 else "unavailable"
    except Exception:
        return "unavailable"


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    deps = {
        "postgres": await _postgres(request),
        "qdrant": await _qdrant(request),
        "llm": await _llm(request),
    }
    status = "ok" if all(v == "ok" for v in deps.values()) else "degraded"
    provider = request.app.state.settings.llm_provider  # NFR-1: a cloud provider is visible
    return {"status": status, **deps, "llm_provider": provider, "version": version("doc-intel")}
