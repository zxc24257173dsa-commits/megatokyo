"""Whatnot Seller API (REST v2) client.

Base URL https://api.whatnot.com/seller-api/rest/v2 ; Bearer token (wn_pat_tk_...).
Rate limit: 10 requests/second per seller per app. We self-throttle below that, honour
Retry-After on 429, and back off exponentially (with jitter) on 429 / 5xx / network errors,
as the official docs recommend. Errors are RFC 9457 problem+json: branch on `type`.
"""
import logging
import random
import threading
import time
import uuid
from collections.abc import Iterator

import httpx

from app.core.config import get_settings
from app.core.errors import AppError

log = logging.getLogger("app.whatnot")
MAX_ATTEMPTS = 5


class WhatnotError(AppError):
    def __init__(self, status: int, problem_type: str, detail: str, extra: dict | None = None):
        super().__init__(f"whatnot_{problem_type}".replace("-", "_"), f"Whatnot API: {detail}",
                         502 if status >= 500 else 409, {"status": status, "type": problem_type, **(extra or {})})
        self.status = status
        self.problem_type = problem_type


class _Throttle:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


_throttle: _Throttle | None = None


def _get_throttle() -> _Throttle:
    global _throttle
    if _throttle is None:
        _throttle = _Throttle(get_settings().whatnot_requests_per_second)
    return _throttle


class WhatnotClient:
    def __init__(self, token: str, base_url: str | None = None, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self._http = httpx.Client(
            base_url=(base_url or get_settings().whatnot_api_base).rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "User-Agent": "megatokyo-erp/1.0"},
            timeout=httpx.Timeout(20.0, connect=10.0), transport=transport,
        )
        self._sleep = sleep
        self.transport = transport  # also used for report downloads (tests); None = real network

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---------- core request with retries ----------
    def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        attempt, backoff = 0, 1.0
        while True:
            attempt += 1
            _get_throttle().wait()
            rid = uuid.uuid4().hex
            try:
                r = self._http.request(method, path, params=params, json=json, headers={"X-Request-ID": rid})
            except httpx.HTTPError as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise WhatnotError(503, "unreachable", f"cannot reach Whatnot ({exc.__class__.__name__})") from exc
                self._sleep(backoff + random.uniform(0, 0.5))
                backoff = min(backoff * 2, 30)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt >= MAX_ATTEMPTS:
                    raise self._error(r, rid)
                wait = float(r.headers.get("Retry-After") or backoff) if r.status_code == 429 else backoff
                log.warning("whatnot retry", extra={"extra_fields": {"status": r.status_code, "path": path,
                                                                      "attempt": attempt, "wait": wait}})
                self._sleep(wait + random.uniform(0, 0.5))
                backoff = min(max(backoff, wait) * 2, 60)
                continue
            if r.status_code >= 400:
                raise self._error(r, rid)
            return r.json() if r.content else {}

    @staticmethod
    def _error(r: httpx.Response, rid: str) -> WhatnotError:
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 422 and isinstance(body.get("detail"), list):
            return WhatnotError(422, "validation-error", "request validation failed", {"errors": body["detail"],
                                                                                        "request_id": rid})
        extra = {k: body[k] for k in ("param", "required_scopes") if k in body}
        return WhatnotError(r.status_code, body.get("type") or f"http-{r.status_code}",
                            body.get("detail") or r.reason_phrase, {**extra, "request_id": rid})

    def get(self, path: str, **params) -> dict:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def paginate(self, path: str, *, limit: int = 50, reverse: bool = True, **params) -> Iterator[dict]:
        """Yields items across pages (newest first by default)."""
        cursor = None
        while True:
            page = self.get(path, limit=limit, cursor=cursor, reverse=str(reverse).lower(), **params)
            yield from page.get("data", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # ---------- endpoints used by the ERP (read-only, plus report generation) ----------
    def me(self) -> dict:
        return self.get("/users/me")

    def order(self, order_id: str) -> dict:
        return self.get(f"/orders/{order_id}")

    def order_items(self, order_id: str) -> list[dict]:
        return list(self.paginate(f"/orders/{order_id}/items", limit=50, reverse=False))

    def shipment(self, shipment_id: str) -> dict:
        return self.get(f"/shipments/{shipment_id}")

    def shipment_items(self, shipment_id: str) -> list[dict]:
        return list(self.paginate(f"/shipments/{shipment_id}/items", limit=50, reverse=False))

    def show(self, show_id: str) -> dict:
        return self.get(f"/shows/{show_id}")

    def show_analytics(self, show_id: str) -> dict:
        return self.get(f"/analytics/shows/{show_id}")


def get_token(db) -> str | None:
    """Env/Secret Manager token wins; otherwise the encrypted token saved from the admin screen."""
    s = get_settings()
    if s.whatnot_api_token:
        return s.whatnot_api_token.get_secret_value()
    from app.integrations.whatnot import account
    return account.stored_token(db)


def client_for(db, transport: httpx.BaseTransport | None = None) -> WhatnotClient:
    token = get_token(db)
    if not token:
        raise AppError("whatnot_not_connected", "Whatnot API token is not configured", 409)
    return WhatnotClient(token, transport=transport)
