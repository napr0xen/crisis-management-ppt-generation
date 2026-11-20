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


CHILD_ROUTES = [
    ("GET", "/v1/projects/{p}/documents/{d}"),
    ("DELETE", "/v1/projects/{p}/documents/{d}"),
    ("GET", "/v1/projects/{p}/jobs/{j}"),
    ("GET", "/v1/projects/{p}/templates/{d}"),
    ("GET", "/v1/projects/{p}/templates/{d}/irs/1"),
    ("GET", "/v1/projects/{p}/mapping-runs/{j}"),
    ("POST", "/v1/projects/{p}/mapping-runs/{j}/edits"),
]
PROJECT_ROUTES = [
    ("GET", "/v1/projects/{p}/documents"),
    ("POST", "/v1/projects/{p}/documents"),
    ("POST", "/v1/projects/{p}/templates"),
    ("POST", "/v1/projects/{p}/mapping-runs"),
    ("GET", "/v1/projects/{p}/artifacts"),
]
# own_project=True: B's own project with A's document/job ids.
CASES = [(*r, False) for r in CHILD_ROUTES + PROJECT_ROUTES] + [(*r, True) for r in CHILD_ROUTES]


def _upload(client, user_id, project_id, data=b"%PDF-1.7 a"):
    return client.post(
        f"/v1/projects/{project_id}/documents",
        files={"file": ("r.pdf", data)},
        headers=headers(user_id),
    )


@pytest.mark.parametrize("method, template, own_project", CASES)
def test_other_users_document_and_job_ids_are_404_identical_to_random(
    client, user_a, user_b, project, method, template, own_project
):
    up = _upload(client, user_a, project).json()
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    p = b_project["id"] if own_project else project
    hdrs = {**headers(user_b), "X-Request-Id": "r1"}

    def call(pid, did, jid):
        path = template.format(p=pid, d=did, j=jid)
        if path.endswith("/templates"):  # JSON body: B names A's document
            return client.request(method, path, headers=hdrs, json={"document_id": str(did)})
        if path.endswith("/edits"):  # valid body: only the run id is foreign
            body = {"edits": [{"op": "clear", "field_id": "a"}]}
            return client.request(method, path, headers=hdrs, json=body)
        if path.endswith("/mapping-runs"):  # B names an id that is not B's template IR
            return client.request(method, path, headers=hdrs, json={"template_ir_id": str(did)})
        return client.request(method, path, headers=hdrs, files={"file": ("r.pdf", b"%PDF-x")})

    as_b = call(p, up["document_id"], up["job_id"])
    random = call(p if own_project else uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert as_b.status_code == random.status_code == 404
    assert as_b.json() == random.json()
    # user A's document is untouched
    r = client.get(f"/v1/projects/{project}/documents/{up['document_id']}", headers=headers(user_a))
    assert r.status_code == 200


def test_dedup_is_per_tenant(client, user_a, user_b, project):
    a_doc = _upload(client, user_a, project).json()["document_id"]
    b_project = client.post("/v1/projects", json={"name": "B"}, headers=headers(user_b)).json()
    b_up = _upload(client, user_b, b_project["id"]).json()
    assert b_up["deduplicated"] is False
    assert b_up["document_id"] != a_doc
    listed = client.get(f"/v1/projects/{b_project['id']}/documents", headers=headers(user_b))
    assert [d["id"] for d in listed.json()["items"]] == [b_up["document_id"]]
