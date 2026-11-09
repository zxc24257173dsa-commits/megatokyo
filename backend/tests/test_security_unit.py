import time

import pyotp
import pytest

from app.core import crypto, security


def test_password_hash_roundtrip_and_argon2id():
    h = security.hash_password("Correct-Horse-42!")
    assert h.startswith("$argon2id$")
    assert security.verify_password("Correct-Horse-42!", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("anything", None)


@pytest.mark.parametrize("pw,ok", [
    ("short", False), ("aaaaaaaaaaaaaaa", False), (" Leading-space-pw1", False),
    ("john.smith-Password1", False), ("Correct-Horse-42!", True),
])
def test_password_policy(pw, ok):
    assert (security.password_policy_errors(pw, email="john.smith@example.com") == []) is ok


def test_encryption_roundtrip_and_context_binding():
    blob = crypto.encrypt("JBSWY3DPEHPK3PXP", context="user:1:mfa")
    assert b"JBSWY3DPEHPK3PXP" not in blob
    assert crypto.decrypt(blob, context="user:1:mfa") == "JBSWY3DPEHPK3PXP"
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob, context="user:2:mfa")  # copied to another user's row
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(tampered, context="user:1:mfa")


def test_access_token_type_is_enforced():
    access, _ = security.create_access_token(5, 1)
    mfa = security.create_mfa_challenge_token(5, 1)
    assert security.decode_token(access, "access")["sub"] == "5"
    assert security.decode_token(access, "mfa") is None
    assert security.decode_token(mfa, "access") is None
    assert security.decode_token(access + "x", "access") is None


def test_totp_drift_and_replay():
    secret = pyotp.random_base32()
    now = time.time()
    step = int(now // 30)
    totp = pyotp.TOTP(secret)
    assert security.verify_totp(secret, totp.at(now), None, now=now) == step
    assert security.verify_totp(secret, totp.at(now - 30), None, now=now) == step - 1   # clock drift ok
    assert security.verify_totp(secret, totp.at(now - 90), None, now=now) is None       # too old
    assert security.verify_totp(secret, totp.at(now), step, now=now) is None            # replay rejected
    assert security.verify_totp(secret, "12345a", None, now=now) is None
