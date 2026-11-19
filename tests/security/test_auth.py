"""Every non-public route needs the bearer secret and a valid X-OWUI-User-Id (ADR-009)."""

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from doc_intel.core.config import Settings
from doc_intel.main import create_app
from tests.conftest import AUTH, PIPE_SECRET

PUBLIC = {"/v1/health"}
# Auth is checked before any DB access, so no services are needed.
SETTINGS = Settings(_env_file=None, database_url="postgresql+asyncpg://x:x@127.0.0.1:1/x")

# Built from the app's OpenAPI schema (every route), so a new route that forgets
# `require_user` fails this test.
PROTECTED = sorted(
    (method.upper(), re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path))
    for path, operations in create_app(SETTINGS).openapi()["paths"].items()
    if path.startswith("/v1") and path not in PUBLIC
    for method in operations
)


@pytest.fixture(scope="module")
def offline_client():
    with TestClient(create_app(SETTINGS)) as c:
        yield c


def test_route_discovery_finds_the_project_routes():
    paths = {(m, re.sub(r"[0-9a-f-]{36}", "{id}", p)) for m, p in PROTECTED}
    assert {
        ("GET", "/v1/projects"),
        ("POST", "/v1/projects"),
        ("GET", "/v1/projects/{id}"),
        ("POST", "/v1/projects/{id}/query"),
    } <= paths


@pytest.mark.parametrize("method, path", PROTECTED)
@pytest.mark.parametrize(
    "auth",
    [
        None,
        "Bearer wrong",
        PIPE_SECRET,
        f"Bearer {PIPE_SECRET} ",
        f"bearer {PIPE_SECRET}",
    ],
)
def test_bad_or_missing_bearer_is_401(offline_client, method, path, auth):
    hdrs = {"X-OWUI-User-Id": "user_1"} | ({"Authorization": auth} if auth else {})
    r = offline_client.request(method, path, headers=hdrs, json={"name": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("method, path", PROTECTED)
@pytest.mark.parametrize("user_id", [None, "", "../x", "a b", "x" * 65])
def test_bad_user_id_is_422(offline_client, method, path, user_id):
    hdrs = dict(AUTH)
    if user_id is not None:
        hdrs["X-OWUI-User-Id"] = user_id
    r = offline_client.request(method, path, headers=hdrs, json={"name": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_health_is_public(offline_client):
    assert offline_client.get("/v1/health").status_code == 200
