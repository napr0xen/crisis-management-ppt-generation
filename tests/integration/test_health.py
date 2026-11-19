import pytest

pytestmark = pytest.mark.integration


def test_live_health_reports_dependencies_ok(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "postgres": "ok",
        "qdrant": "ok",
        "llm": "ok",
        "llm_provider": "ollama",
        "version": "0.1.0",
    }
