"""The signed download link is the only credential on `GET /v1/artifacts/{id}/download`."""

import time
from urllib.parse import parse_qs, urlsplit

import pytest

from doc_intel.core.config import get_settings
from doc_intel.core.security import sign_artifact
from doc_intel.storage.local import LocalStorage
from tests.conftest import AUTH
from tests.integration.test_artifacts_api import ctx_of, seed_artifact

pytestmark = pytest.mark.integration

BODY = b"PK\x03\x04 artifact bytes"


def url(artifact_id, exp, sig, extra="") -> str:
    return f"/v1/artifacts/{artifact_id}/download?exp={exp}&sig={sig}{extra}"


def signed(artifact, now=None, user=None) -> str:
    exp, sig = sign_artifact(artifact.id, user or artifact.user_id, now)
    return url(artifact.id, exp, sig)


@pytest.fixture
async def artifact(session, user_a, project):
    return await seed_artifact(session, ctx_of(user_a, project), data=BODY)


async def test_valid_link_streams_the_file_with_safe_headers(client, artifact):
    r = client.get(signed(artifact))
    assert r.status_code == 200
    assert r.content == BODY
    assert r.headers["content-type"].endswith("presentationml.presentation")
    assert r.headers["content-disposition"] == 'attachment; filename="out.pptx"'
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["content-length"] == str(len(BODY))


@pytest.mark.parametrize(
    "fmt, subtype", [("docx", "wordprocessingml.document"), ("xlsx", "spreadsheetml.sheet")]
)
async def test_content_type_per_format(client, session, user_a, project, fmt, subtype):
    row = await seed_artifact(session, ctx_of(user_a, project), fmt=fmt, filename=f"o.{fmt}")
    r = client.get(signed(row))
    assert r.headers["content-type"] == f"application/vnd.openxmlformats-officedocument.{subtype}"


async def test_bearer_header_is_neither_needed_nor_changes_the_outcome(client, artifact):
    assert client.get(signed(artifact), headers=AUTH).status_code == 200
    exp, _ = sign_artifact(artifact.id, artifact.user_id)
    assert client.get(url(artifact.id, exp, "0" * 64), headers=AUTH).status_code == 403


async def test_altered_signature_is_403(client, artifact):
    exp, sig = sign_artifact(artifact.id, artifact.user_id)
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    r = client.get(url(artifact.id, exp, flipped))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "signature_invalid"


async def test_altered_exp_is_403(client, artifact):
    exp, sig = sign_artifact(artifact.id, artifact.user_id)
    assert client.get(url(artifact.id, exp + 3600, sig)).status_code == 403


async def test_missing_signature_is_403(client, artifact):
    assert client.get(f"/v1/artifacts/{artifact.id}/download").status_code == 403
    exp, _ = sign_artifact(artifact.id, artifact.user_id)
    assert client.get(f"/v1/artifacts/{artifact.id}/download?exp={exp}").status_code == 403


async def test_signature_of_another_artifact_is_403(client, session, user_a, project, artifact):
    other = await seed_artifact(session, ctx_of(user_a, project))
    other_query = parse_qs(urlsplit(signed(other)).query)
    r = client.get(url(artifact.id, other_query["exp"][0], other_query["sig"][0]))
    assert r.status_code == 403


async def test_token_signed_for_another_user_is_403(client, artifact, user_b):
    assert client.get(signed(artifact, user=user_b)).status_code == 403


async def test_expired_link_is_410(client, artifact):
    r = client.get(signed(artifact, now=int(time.time()) - 10_000))
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "link_expired"


async def test_forged_expired_link_is_403_not_410(client, artifact):
    past = int(time.time()) - 10_000
    assert client.get(url(artifact.id, past, "0" * 64)).status_code == 403


def test_unknown_artifact_is_404(client):
    import uuid

    r = client.get(url(uuid.uuid4(), int(time.time()) + 60, "0" * 64))
    assert r.status_code == 404


async def test_oversized_signature_is_rejected(client, artifact):
    assert client.get(url(artifact.id, 1, "a" * 65)).status_code == 422


async def test_filename_cannot_inject_header_lines(client, session, user_a, project):
    row = await seed_artifact(
        session, ctx_of(user_a, project), filename='a"\r\nSet-Cookie: x=1\\b.pptx'
    )
    r = client.get(signed(row))
    assert r.status_code == 200
    disposition = r.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert disposition.count('"') == 2
    assert "set-cookie" not in r.headers


async def test_only_the_rows_storage_key_is_served(client, session, user_a, project, artifact):
    storage = LocalStorage(get_settings().storage_root)
    storage.put_bytes("secret-elsewhere.bin", b"SECRET")
    for extra in ("&key=secret-elsewhere.bin", "&key=../../x", "&storage_key=../../etc/passwd"):
        exp, sig = sign_artifact(artifact.id, artifact.user_id)
        r = client.get(url(artifact.id, exp, sig, extra))
        assert r.status_code == 200
        assert r.content == BODY


@pytest.mark.parametrize("key", ["../outside", "a/../../outside", "/etc/passwd", ""])
def test_local_storage_rejects_escaping_keys(key):
    storage = LocalStorage(get_settings().storage_root)
    with pytest.raises(ValueError):
        storage.open_read(key)
