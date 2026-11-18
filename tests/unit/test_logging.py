import json

import structlog

from doc_intel.core.logging import FORBIDDEN_KEYS, configure_logging, redact_forbidden


def test_redaction_drops_every_forbidden_key():
    event = {"event": "x", "document_id": "d1", **{k: "SENTINEL" for k in FORBIDDEN_KEYS}}
    out = redact_forbidden(None, "info", dict(event))
    assert "SENTINEL" not in json.dumps(out)
    assert out["document_id"] == "d1"


def test_forbidden_keys_cover_guidelines():
    assert {"text", "quote", "prompt", "completion", "content", "filename", "authorization"} <= (
        FORBIDDEN_KEYS
    )


def test_configured_logger_emits_json_without_forbidden_values(capsys):
    configure_logging("INFO")
    structlog.get_logger().info("upload_received", document_id="d1", filename="secret.pdf")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "upload_received"
    assert record["level"] == "info"
    assert "ts" in record
    assert "secret.pdf" not in line
