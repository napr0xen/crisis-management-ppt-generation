"""App wiring that needs no running services: dependencies point at closed ports."""

import pytest
from fastapi.testclient import TestClient

from doc_intel.core.config import Settings
from doc_intel.main import create_app


@pytest.fixture
def offline_client():
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://x:x@127.0.0.1:1/x",
        qdrant_url="http://127.0.0.1:1",
        llm_base_url="http://127.0.0.1:1",
    )
    with TestClient(create_app(settings)) as c:
        yield c


def test_health_probes_an_openai_compatible_llm_with_its_key():
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200 if request.url.path == "/v1/models" else 503)

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://x:x@127.0.0.1:1/x",
        llm_provider="openai",
        llm_base_url="https://llm.example.com/v1",
        llm_api_key="sk-test",
    )
    with TestClient(create_app(settings)) as c:
        c.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        body = c.get("/v1/health").json()
    assert (body["llm"], body["llm_provider"]) == ("ok", "openai")  # NFR-1
    [probe] = [r for r in seen if r.url.host == "llm.example.com"]
    assert probe.headers["authorization"] == "Bearer sk-test"


def test_health_is_public_and_reports_degraded(offline_client):
    r = offline_client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["postgres"] != "ok"
    assert body["qdrant"] != "ok"
    assert body["llm"] == "unavailable"
    assert body["version"] == "0.1.0"


def test_request_id_is_generated(offline_client):
    r = offline_client.get("/v1/health")
    assert len(r.headers["X-Request-Id"]) >= 16


def test_request_id_is_echoed(offline_client):
    r = offline_client.get("/v1/health", headers={"X-Request-Id": "req-123"})
    assert r.headers["X-Request-Id"] == "req-123"


def test_unsafe_request_id_is_replaced(offline_client):
    r = offline_client.get("/v1/health", headers={"X-Request-Id": "bad\nid"})
    assert r.headers["X-Request-Id"] != "bad\nid"


def test_errors_use_the_envelope(offline_client):
    r = offline_client.get("/v1/projects", headers={"X-Request-Id": "req-9"})
    assert r.status_code == 401
    assert r.json() == {
        "error": {"code": "unauthorized", "message": "Unauthorized.", "request_id": "req-9"}
    }


def test_unknown_route_uses_the_envelope(offline_client):
    r = offline_client.get("/v1/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
