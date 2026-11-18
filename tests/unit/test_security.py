import uuid

import pytest

from doc_intel.core.errors import LinkExpired, SignatureInvalid
from doc_intel.core.security import USER_ID_RE, sign_artifact, verify_artifact_signature

ARTIFACT = uuid.UUID("3f6c1a0e-8b1d-4b55-9a57-2f1c0d7e9b10")


@pytest.mark.parametrize("user_id", ["abc", "A-b_9", "x" * 64])
def test_user_id_regex_accepts(user_id):
    assert USER_ID_RE.fullmatch(user_id)


@pytest.mark.parametrize("user_id", ["", "x" * 65, "../etc", "a/b", "a b", "a|b", "ü"])
def test_user_id_regex_rejects(user_id):
    assert not USER_ID_RE.fullmatch(user_id)


def test_sign_verify_round_trip():
    exp, sig = sign_artifact(ARTIFACT, "user_1", now=1_000)
    assert exp == 1_000 + 900
    verify_artifact_signature(ARTIFACT, "user_1", exp, sig, now=1_500)


@pytest.mark.parametrize(
    "artifact, user, exp_delta, sig_mutation",
    [
        (uuid.uuid4(), "user_1", 0, False),  # other artifact
        (ARTIFACT, "user_2", 0, False),  # other owner
        (ARTIFACT, "user_1", 60, False),  # extended expiry
        (ARTIFACT, "user_1", 0, True),  # altered signature
    ],
)
def test_tampering_is_rejected(artifact, user, exp_delta, sig_mutation):
    exp, sig = sign_artifact(ARTIFACT, "user_1", now=1_000)
    if sig_mutation:
        sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    with pytest.raises(SignatureInvalid):
        verify_artifact_signature(artifact, user, exp + exp_delta, sig, now=1_500)


def test_expired_link():
    exp, sig = sign_artifact(ARTIFACT, "user_1", now=1_000)
    with pytest.raises(LinkExpired):
        verify_artifact_signature(ARTIFACT, "user_1", exp, sig, now=exp + 1)


def test_forged_expired_link_reports_signature_not_expiry():
    exp, _ = sign_artifact(ARTIFACT, "user_1", now=1_000)
    with pytest.raises(SignatureInvalid):
        verify_artifact_signature(ARTIFACT, "user_1", exp, "deadbeef", now=exp + 1)
