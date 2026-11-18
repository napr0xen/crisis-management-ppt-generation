import pytest

from tests.conftest import headers

pytestmark = pytest.mark.integration


def test_create_get_list(client, user_a):
    r = client.post("/v1/projects", json={"name": "Q3 incident"}, headers=headers(user_a))
    assert r.status_code == 201
    created = r.json()
    assert created["name"] == "Q3 incident"
    assert created["openwebui_chat_id"] is None
    assert created["document_count"] == 0

    got = client.get(f"/v1/projects/{created['id']}", headers=headers(user_a))
    assert got.status_code == 200
    assert got.json() == created

    listed = client.get("/v1/projects", headers=headers(user_a)).json()["items"]
    assert [p["id"] for p in listed] == [created["id"]]


def test_list_is_newest_first_and_limited(client, user_a):
    ids = [
        client.post("/v1/projects", json={"name": f"p{i}"}, headers=headers(user_a)).json()["id"]
        for i in range(3)
    ]
    listed = client.get("/v1/projects?limit=2", headers=headers(user_a)).json()["items"]
    assert [p["id"] for p in listed] == ids[::-1][:2]


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "x" * 201}, {"name": "a", "extra": 1}])
def test_invalid_body_is_422(client, user_a, body):
    r = client.post("/v1/projects", json=body, headers=headers(user_a))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
