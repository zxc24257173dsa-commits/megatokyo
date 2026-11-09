"""File storage behind a small interface (local disk now; Google Cloud Storage in Phase 12).

Files are never public. The browser gets a short-lived signed URL
(/api/v1/files/<key>?exp=..&sig=..) so <img> tags work without exposing the login token.
"""
import hashlib
import hmac
import os
import time
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError

IMAGE_TYPES = {  # magic bytes -> (content type, extension)
    b"\xff\xd8\xff": ("image/jpeg", "jpg"),
    b"\x89PNG\r\n\x1a\n": ("image/png", "png"),
}


def sniff_image(data: bytes) -> tuple[str, str]:
    """Trust the file's bytes, not its name or the browser-declared type."""
    for magic, info in IMAGE_TYPES.items():
        if data.startswith(magic):
            return info
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise AppError("unsupported_file_type", "Only JPEG, PNG or WebP images are allowed", 415)


def _root() -> Path:
    return Path(get_settings().storage_dir).resolve()


def _safe_path(key: str) -> Path:
    if not key or ".." in key.split("/") or key.startswith("/"):
        raise AppError("not_found", "File not found", 404)
    path = (_root() / key).resolve()
    if _root() not in path.parents:
        raise AppError("not_found", "File not found", 404)
    return path


def save(prefix: str, data: bytes, ext: str) -> str:
    key = f"{prefix}/{uuid.uuid4().hex}.{ext}"
    path = _safe_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # atomic
    return key


def path_for(key: str) -> Path:
    path = _safe_path(key)
    if not path.is_file():
        raise AppError("not_found", "File not found", 404)
    return path


def delete(key: str) -> None:
    try:
        _safe_path(key).unlink(missing_ok=True)
    except AppError:
        pass


def _sig(key: str, exp: int) -> str:
    secret = ("files:" + get_settings().jwt_secret.get_secret_value()).encode()
    return hmac.new(secret, f"{key}|{exp}".encode(), hashlib.sha256).hexdigest()


def signed_url(key: str) -> str:
    exp = int(time.time()) + get_settings().file_url_ttl_seconds
    return f"/api/v1/files/{key}?exp={exp}&sig={_sig(key, exp)}"


def verify_signature(key: str, exp: int, sig: str) -> bool:
    return exp >= int(time.time()) and hmac.compare_digest(_sig(key, exp), sig)
