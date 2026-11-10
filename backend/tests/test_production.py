"""Phase 12: production pieces - Cloud Storage backend, scheduler endpoints, production config checks."""
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, get_settings
from app.services import storage
from tests import whatnot_fake as W
from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import EDITOR

SECRET = "s" * 40


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_from_string(self, data, content_type=None):
        self.store[self.name] = (data, content_type)

    def download_as_bytes(self):
        from google.api_core.exceptions import NotFound
        if self.name not in self.store:
            raise NotFound("no such object")
        return self.store[self.name][0]

    def delete(self):
        from google.api_core.exceptions import NotFound
        if self.store.pop(self.name, None) is None:
            raise NotFound("no such object")


class FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, name):
        return FakeBlob(self.store, name)


@pytest.fixture
def gcs(monkeypatch):
    bucket = FakeBucket()
    s = get_settings()
    monkeypatch.setattr(s, "storage_backend", "gcs")
    monkeypatch.setattr(s, "storage_bucket", "megatokyo-files")
    monkeypatch.setattr(storage, "_gcs", lambda: bucket)
    return bucket


PDF = b"%PDF-1.7 receipt"


def test_receipts_and_images_go_to_the_bucket_and_stream_back(api, db, gcs):
    _, mgr = token_for(api, db, "tw_manager")
    r = api.post("/api/v1/expenses", headers={**auth_header(mgr), **idem()},
                 data={"expense_date": "2026-11-05", "category": "rent", "amount": "10", "currency": "USD"},
                 files={"receipt": ("r.pdf", PDF, "application/pdf")})
    assert r.status_code == 201, r.text
    [(key, (data, ctype))] = gcs.store.items()
    assert key.startswith("receipts/") and data == PDF and ctype == "application/pdf"
    f = api.get(r.json()["receipt_url"])
    assert f.status_code == 200 and f.content == PDF and f.headers["content-type"] == "application/pdf"
    assert f.headers["x-content-type-options"] == "nosniff"
    storage.delete(key)
    assert gcs.store == {}
    assert api.get(r.json()["receipt_url"]).status_code == 404
    with pytest.raises(Exception):
        storage.read("../etc/passwd")


def test_import_files_are_read_from_the_bucket(api, db, gcs):
    _, tok = token_for(api, db, *EDITOR)
    csv = "SKU,Product Name\nGCS-1,From the bucket\n".encode()
    r = api.post("/api/v1/imports", headers=auth_header(tok), data={"import_type": "sku_master"},
                 files={"file": ("skus.csv", csv, "text/csv")})
    assert r.status_code == 201, r.text
    assert any(k.startswith("imports/") for k in gcs.store)
    c = api.post(f"/api/v1/imports/{r.json()['id']}/commit", headers=auth_header(tok), json={})
    assert c.status_code == 200, c.text  # commit re-reads the file from the bucket


def test_cron_endpoints(api, db, monkeypatch):
    s = get_settings()
    assert api.post("/api/v1/internal/cron/webhooks").status_code == 404  # off unless configured
    monkeypatch.setattr(s, "cron_secret", SecretStr(SECRET))
    assert api.post("/api/v1/internal/cron/webhooks").status_code == 403
    assert api.post("/api/v1/internal/cron/webhooks", headers={"X-Cron-Secret": "wrong"}).status_code == 403
    r = api.post("/api/v1/internal/cron/webhooks", headers={"X-Cron-Secret": SECRET})
    assert r.status_code == 200 and r.json() == {"skipped": "whatnot not connected"}
    fake = W.FakeWhatnot()
    from app.integrations.whatnot import client as wn
    monkeypatch.setattr(wn, "get_token", lambda db: "wn_pat_tk_test")
    monkeypatch.setattr(wn, "client_for", lambda db: fake.client())
    r = api.post("/api/v1/internal/cron/whatnot-sync", headers={"X-Cron-Secret": SECRET}, params={"days": 1})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    # people can't call it with their login
    _, mgr = token_for(api, db, "tw_manager")
    assert api.post("/api/v1/internal/cron/whatnot-sync", headers=auth_header(mgr)).status_code == 403


def test_production_settings_refuse_unsafe_values():
    good = dict(env="production", jwt_secret="j" * 48, data_encryption_key="a2V5LWtleS1rZXkta2V5LWtleS1rZXkta2V5LWtleSE=",
                storage_backend="gcs", storage_bucket="b", public_base_url="https://erp.example.com",
                cron_secret=SECRET, cookie_secure=True)
    Settings(**good)
    for bad in ({"storage_backend": "local"}, {"storage_bucket": None}, {"public_base_url": "http://x"},
                {"cron_secret": "short"}, {"cookie_secure": False}, {"jwt_secret": Settings().jwt_secret}):
        with pytest.raises(ValidationError):
            Settings(**{**good, **bad})
