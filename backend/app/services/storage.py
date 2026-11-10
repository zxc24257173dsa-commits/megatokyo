"""File storage behind a small interface: local disk (dev, single server) or a private Google Cloud
Storage bucket (production on Cloud Run, whose own disk is wiped on every restart).

Files are never public. The browser gets a short-lived signed URL
(/api/v1/files/<key>?exp=..&sig=..) so <img> tags work without exposing the login token; the API
reads the file and streams it.
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


def _check_key(key: str) -> str:
    if not key or ".." in key.split("/") or key.startswith("/") or "\\" in key:
        raise AppError("not_found", "File not found", 404)
    return key


def _safe_path(key: str) -> Path:
    _check_key(key)
    path = (_root() / key).resolve()
    if _root() not in path.parents:
        raise AppError("not_found", "File not found", 404)
    return path


_bucket = None


def _gcs():
    """The private bucket (credentials: the Cloud Run service account, via application default credentials)."""
    global _bucket
    if _bucket is None:
        from google.cloud import storage as gcs
        name = get_settings().storage_bucket
        if not name:
            raise RuntimeError("ERP_STORAGE_BUCKET is not set")
        _bucket = gcs.Client().bucket(name)
    return _bucket


def _is_gcs() -> bool:
    return get_settings().storage_backend == "gcs"


def save(prefix: str, data: bytes, ext: str) -> str:
    key = _check_key(f"{prefix}/{uuid.uuid4().hex}.{ext}")
    if _is_gcs():
        _gcs().blob(key).upload_from_string(data, content_type=CONTENT_TYPES.get(ext, "application/octet-stream"))
        return key
    path = _safe_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # atomic
    return key


def read(key: str) -> bytes:
    if _is_gcs():
        from google.api_core.exceptions import NotFound
        try:
            return _gcs().blob(_check_key(key)).download_as_bytes()
        except NotFound:
            raise AppError("not_found", "File not found", 404) from None
    path = _safe_path(key)
    if not path.is_file():
        raise AppError("not_found", "File not found", 404)
    return path.read_bytes()


def delete(key: str) -> None:
    try:
        if _is_gcs():
            from google.api_core.exceptions import NotFound
            try:
                _gcs().blob(_check_key(key)).delete()
            except NotFound:
                pass
            return
        _safe_path(key).unlink(missing_ok=True)
    except AppError:
        pass


CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf",
                 "csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def content_type(key: str) -> str:
    return CONTENT_TYPES.get(key.rsplit(".", 1)[-1].lower(), "application/octet-stream")


def _sig(key: str, exp: int) -> str:
    secret = ("files:" + get_settings().jwt_secret.get_secret_value()).encode()
    return hmac.new(secret, f"{key}|{exp}".encode(), hashlib.sha256).hexdigest()


def signed_url(key: str) -> str:
    exp = int(time.time()) + get_settings().file_url_ttl_seconds
    return f"/api/v1/files/{key}?exp={exp}&sig={_sig(key, exp)}"


def verify_signature(key: str, exp: int, sig: str) -> bool:
    return exp >= int(time.time()) and hmac.compare_digest(_sig(key, exp), sig)
