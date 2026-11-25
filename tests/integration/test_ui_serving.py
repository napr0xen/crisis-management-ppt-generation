"""CORS for the dev UI and the built UI served at `/` (Phase 19)."""

import pytest
from fastapi.testclient import TestClient

from doc_intel import main
from tests.conftest import headers

pytestmark = pytest.mark.integration

ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


@pytest.mark.parametrize("origin", ORIGINS)
def test_preflight_allows_the_dev_ui(client, origin):
    r = client.options(
        "/v1/library/templates",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,x-owui-user-id,"
            "x-owui-chat-id,x-request-id",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    methods = r.headers["access-control-allow-methods"]
    assert {"GET", "POST", "DELETE", "OPTIONS"} <= {m.strip() for m in methods.split(",")}
    allowed = r.headers["access-control-allow-headers"].lower()
    for h in ("authorization", "content-type", "x-owui-user-id", "x-owui-chat-id", "x-request-id"):
        assert h in allowed


def test_other_origin_gets_no_cors_headers(client):
    r = client.get("/v1/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_error_responses_carry_cors_headers_and_expose_the_request_id(client, user_a):
    origin = ORIGINS[0]
    r = client.get("/v1/library/templates", headers={"Origin": origin})  # no bearer: 401
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == origin
    assert "x-request-id" in r.headers["access-control-expose-headers"].lower()
    ok = client.get("/v1/library/templates", headers={**headers(user_a), "Origin": origin})
    assert ok.headers["access-control-allow-origin"] == origin


@pytest.fixture
def ui_client(migrated_db, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>UI</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path)
    with TestClient(main.create_app()) as c:
        yield c


def test_built_ui_is_served_at_root(ui_client):
    r = ui_client.get("/")
    assert r.status_code == 200 and "UI" in r.text
    assert ui_client.get("/assets/app.js").status_code == 200


def test_ui_does_not_shadow_routes_added_later(ui_client):
    # tests and plugins add routes after create_app(); a catch-all "/" mount would swallow them
    ui_client.app.add_api_route("/v1/added-later", lambda: {"ok": True})
    assert ui_client.get("/v1/added-later").json() == {"ok": True}
    assert ui_client.get("/not-a-ui-file").status_code == 404


def test_api_still_works_beside_the_ui(ui_client, user_a):
    r = ui_client.get("/v1/health")
    assert r.status_code == 200 and r.json()["version"]
    r = ui_client.get("/v1/projects", headers=headers(user_a))
    assert r.status_code == 200


def test_unknown_v1_path_is_still_the_json_404_envelope(ui_client):
    r = ui_client.get("/v1/nope", headers={"X-Request-Id": "r1"})
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "not_found", "message": "Resource not found.",
                                  "request_id": "r1"}}  # fmt: skip


def test_without_a_build_nothing_is_mounted(migrated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path / "missing")
    with TestClient(main.create_app()) as c:
        assert c.get("/").status_code == 404
