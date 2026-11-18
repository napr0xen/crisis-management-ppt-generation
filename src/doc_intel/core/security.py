"""Pipe auth (ADR-009) and signed artifact URLs."""

import hashlib
import hmac
import re
import time
from uuid import UUID

from fastapi import Header, Request

from doc_intel.core.config import get_settings
from doc_intel.core.errors import LinkExpired, SignatureInvalid, Unauthorized, ValidationFailed

USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def require_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_owui_user_id: str | None = Header(default=None),
) -> str:
    """Shared-secret bearer + trusted identity header. Returns the caller's `user_id`."""
    expected = f"Bearer {request.app.state.settings.pipe_shared_secret.get_secret_value()}"
    if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise Unauthorized()
    if not x_owui_user_id or not USER_ID_RE.fullmatch(x_owui_user_id):
        raise ValidationFailed("Invalid X-OWUI-User-Id.")
    return x_owui_user_id


def _signature(artifact_id: UUID, user_id: str, exp: int) -> str:
    key = get_settings().signing_secret.get_secret_value().encode()
    return hmac.new(key, f"{artifact_id}|{user_id}|{exp}".encode(), hashlib.sha256).hexdigest()


def sign_artifact(artifact_id: UUID, user_id: str, now: int | None = None) -> tuple[int, str]:
    exp = int(now if now is not None else time.time()) + get_settings().signed_url_ttl_s
    return exp, _signature(artifact_id, user_id, exp)


def verify_artifact_signature(
    artifact_id: UUID, user_id: str, exp: int, sig: str, now: int | None = None
) -> None:
    # Signature before expiry, so a forged link never learns anything from the expiry check.
    if not hmac.compare_digest(sig.encode(), _signature(artifact_id, user_id, exp).encode()):
        raise SignatureInvalid()
    if int(now if now is not None else time.time()) > exp:
        raise LinkExpired()
