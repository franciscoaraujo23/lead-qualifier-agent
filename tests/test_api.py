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


def test_stats_reflects_a_qualified_lead(monkeypatch, client):
    # Fresh repo so the count is deterministic regardless of other tests.
    from lead_qualifier import api
    from lead_qualifier.config import settings
    from lead_qualifier.persistence import InMemoryRepository
    monkeypatch.setattr(api, "_repo", InMemoryRepository())

    before = client.get("/stats").json()
    assert before["leads"] == 0

    client.post("/qualify", json={"domain": "acme.com"})
    after = client.get("/stats")
    assert after.status_code == 200
    body = after.json()
    assert body["leads"] == 1
    assert body["llm_calls"] >= 1
    assert body["requests_completed"] == 1
    assert body["daily_ceiling_usd"] == settings.daily_cost_ceiling_usd
    assert body["avg_latency_ms"] is not None


def test_empty_token_means_open_not_locked(monkeypatch, client):
    """docker compose passes an empty string when no token is set. Treated as
    None it would be a real token nobody knows, and a keyless `docker compose up`
    would answer 401 to every request. Empty must mean open."""
    from lead_qualifier.api import settings
    monkeypatch.setattr(settings, "webhook_auth_token", "")
    r = client.post("/qualify", json={"domain": "acme.com"})
    assert r.status_code == 200


def test_stats_is_behind_auth(monkeypatch, client):
    from lead_qualifier.api import settings as api_settings
    monkeypatch.setattr(api_settings, "webhook_auth_token", "secret")
    assert client.get("/stats").status_code == 401
    assert client.get("/stats", headers={"x-auth-token": "secret"}).status_code == 200
