import uuid

import pytest

from tests.conftest import headers

pytestmark = pytest.mark.integration


def test_other_users_project_is_404_identical_to_random_id(client, user_a, user_b):
    pid = client.post("/v1/projects", json={"name": "A"}, headers=headers(user_a)).json()["id"]

    as_b = client.get(f"/v1/projects/{pid}", headers={**headers(user_b), "X-Request-Id": "r1"})
    random = client.get(
        f"/v1/projects/{uuid.uuid4()}", headers={**headers(user_b), "X-Request-Id": "r1"}
    )
    assert as_b.status_code == random.status_code == 404
    assert as_b.json() == random.json()


def test_list_shows_only_own_projects(client, user_a, user_b):
    client.post("/v1/projects", json={"name": "A"}, headers=headers(user_a))
    assert client.get("/v1/projects", headers=headers(user_b)).json()["items"] == []
