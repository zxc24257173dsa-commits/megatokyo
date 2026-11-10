import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(engine):
    from app.main import app
    return TestClient(app)


def test_liveness(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readiness_reports_schema_version(client):
    r = client.get("/api/v1/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "schema_version": "0014"}


def test_error_envelope_and_request_id(client):
    r = client.get("/api/v1/does-not-exist", headers={"X-Request-ID": "abc123"})
    assert r.status_code == 404
    assert r.headers["X-Request-ID"] == "abc123"
    body = r.json()["error"]
    assert body["code"] == "http_404" and body["request_id"] == "abc123"
