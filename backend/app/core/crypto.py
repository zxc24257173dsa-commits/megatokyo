"""Authenticated encryption for secrets stored in the database
(MFA secrets now, Whatnot API tokens in Phase 5).

Format: version(1 byte) | nonce(12 bytes) | AES-256-GCM ciphertext+tag.
The version byte allows key rotation later without breaking old rows.
"""
import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_VERSION = b"\x01"


class DecryptionError(Exception):
    pass


def _key() -> bytes:
    key = base64.b64decode(get_settings().data_encryption_key.get_secret_value())
    if len(key) != 32:
        raise ValueError("ERP_DATA_ENCRYPTION_KEY must be base64 of exactly 32 bytes")
    return key


def encrypt(plaintext: str, *, context: str) -> bytes:
    """`context` (e.g. "user:12:mfa") is bound as associated data, so a ciphertext copied
    to another row/purpose fails to decrypt."""
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode(), context.encode())
    return _VERSION + nonce + ct


def decrypt(blob: bytes, *, context: str) -> str:
    if not blob or blob[:1] != _VERSION:
        raise DecryptionError("unknown ciphertext version")
    try:
        return AESGCM(_key()).decrypt(blob[1:13], blob[13:], context.encode()).decode()
    except InvalidTag as exc:
        raise DecryptionError("ciphertext failed authentication") from exc
