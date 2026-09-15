"""API tests via FastAPI TestClient — offline (mock provider, enrichment patched)."""

import pytest
from fastapi.testclient import TestClient

import lead_qualifier.pipeline as pipe
from lead_qualifier.schemas import CompanyProfile, EnrichmentSource


@pytest.fixture(autouse=True)
def _patch_enrich(monkeypatch):
    async def fake_enrich(domain):
        return CompanyProfile(
            domain=domain,
            resolved=True,
            technologies=["nginx"],
            sources_ok=list(EnrichmentSource),
        )
    monkeypatch.setattr(pipe, "enrich_domain", fake_enrich)


@pytest.fixture
def client():
    from lead_qualifier.api import app
    return TestClient(app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_qualify_happy_path(client):
    r = client.post("/qualify", json={"domain": "acme.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["domain"] == "acme.com"
    assert 0 <= body["score"]["score"] <= 100
    assert body["tier"] in ("hot", "warm", "cold")
    assert body["trace_id"]


def test_qualify_normalizes_and_validates_domain(client):
    r = client.post("/qualify", json={"domain": "https://Acme.com/pricing"})
    assert r.status_code == 200
    assert r.json()["domain"] == "acme.com"

    bad = client.post("/qualify", json={"domain": "notadomain"})
    assert bad.status_code == 422  # pydantic request validation


def test_auth_token_enforced(monkeypatch, client):
    from lead_qualifier.api import settings
    monkeypatch.setattr(settings, "webhook_auth_token", "secret")

    denied = client.post("/qualify", json={"domain": "acme.com"})
    assert denied.status_code == 401

    ok = client.post(
        "/qualify", json={"domain": "acme.com"}, headers={"x-auth-token": "secret"}
    )
    assert ok.status_code == 200
