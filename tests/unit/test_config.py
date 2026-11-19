import pytest
from pydantic import ValidationError

from doc_intel.core.config import Settings


def test_missing_required_secrets_fail_fast(monkeypatch):
    monkeypatch.delenv("PIPE_SHARED_SECRET", raising=False)
    monkeypatch.delenv("SIGNING_SECRET", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    missing = {e["loc"][0] for e in exc.value.errors()}
    assert {"pipe_shared_secret", "signing_secret"} <= missing


@pytest.mark.parametrize("field", ["pipe_shared_secret", "signing_secret"])
@pytest.mark.parametrize("value", ["", "x" * 31])
def test_short_secrets_fail_fast(field, value):
    # An empty SIGNING_SECRET would make every signed download link forgeable.
    secrets = {"pipe_shared_secret": "p" * 32, "signing_secret": "s" * 32, field: value}
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **secrets)
    assert {e["loc"][0] for e in exc.value.errors()} == {field}


def test_defaults_match_development_guidelines(monkeypatch):
    for var in ("DATABASE_URL", "QDRANT_URL", "STORAGE_ROOT", "SIGNED_URL_TTL_S", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+asyncpg://doc_intel:doc_intel@127.0.0.1:5432/doc_intel"
    assert s.qdrant_url == "http://127.0.0.1:6333"
    assert str(s.storage_root) == "data/storage"
    assert s.signed_url_ttl_s == 900
    assert s.log_level == "INFO"
