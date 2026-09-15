"""Enrichment tests — fully offline. Real parsing against fabricated responses;
orchestrator aggregation against simulated sources.
"""

import httpx
import pytest

import lead_qualifier.enrichment as enr
from lead_qualifier import EnrichmentEmptyError, EnrichmentSource
from lead_qualifier.enrichment.site_source import parse_site
from lead_qualifier.enrichment.tech_source import fingerprint
from lead_qualifier.enrichment.whois_source import _parse_event, _parse_registrar
from lead_qualifier.schemas import DnsInfo, WhoisInfo


def _resp(text="", headers=None, status=200):
    return httpx.Response(status, headers=headers or {}, text=text)


# --- parsing ---------------------------------------------------------------


def test_parse_site_extracts_title_and_description():
    html = """
    <html><head><title>  Acme Inc  </title>
    <meta name="description" content="We make widgets."></head>
    <body><script>var x=1</script><p>Hello world</p></body></html>
    """
    info = parse_site(_resp(html))
    assert info.title == "Acme Inc"
    assert info.description == "We make widgets."
    assert "Hello world" in info.text_excerpt
    assert "var x" not in info.text_excerpt  # script stripped
    assert info.status_code == 200


def test_fingerprint_from_headers_and_html():
    resp = _resp(
        text='<link href="/wp-content/themes/x.css"><script src="/_next/static/a.js">',
        headers={"server": "nginx/1.25", "x-powered-by": "PHP/8.2"},
    )
    tech = fingerprint(resp)
    assert "nginx" in tech
    assert "PHP" in tech
    assert "WordPress" in tech
    assert "Next.js" in tech


def test_fingerprint_empty_when_no_markers():
    assert fingerprint(_resp(text="<html></html>", headers={})) == []


def test_whois_parse_event_and_registrar():
    events = [{"eventAction": "registration", "eventDate": "1997-09-15T04:00:00Z"}]
    assert _parse_event(events, "registration").year == 1997
    assert _parse_event(events, "expiration") is None

    entities = [
        {
            "roles": ["registrar"],
            "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar"]]],
        }
    ]
    registrar, _ = _parse_registrar(entities)
    assert registrar == "Example Registrar"


# --- orchestrator best-effort aggregation ----------------------------------


async def _fake_dns_ok(domain, timeout):
    return DnsInfo(a_records=["1.2.3.4"], ns_records=["ns1.x"])


async def _fake_whois_ok(domain, client):
    return WhoisInfo(registrar="Example Registrar")


async def _fake_raise(*args, **kwargs):
    raise RuntimeError("source down")


async def _fake_fetch_ok(domain, client):
    return _resp(text="<title>X</title>", headers={"server": "nginx"})


async def test_orchestrator_all_sources_ok(monkeypatch):
    monkeypatch.setattr(enr, "lookup_dns", _fake_dns_ok)
    monkeypatch.setattr(enr, "lookup_whois", _fake_whois_ok)
    monkeypatch.setattr(enr, "fetch_site", _fake_fetch_ok)

    profile = await enr.enrich_domain("acme.com", timeout=1.0)
    assert profile.resolved is True
    assert EnrichmentSource.DNS in profile.sources_ok
    assert EnrichmentSource.SCRAPE in profile.sources_ok
    assert EnrichmentSource.TECH_FINGERPRINT in profile.sources_ok
    assert "nginx" in profile.technologies


async def test_orchestrator_site_down_marks_both_failed(monkeypatch):
    monkeypatch.setattr(enr, "lookup_dns", _fake_dns_ok)
    monkeypatch.setattr(enr, "lookup_whois", _fake_raise)
    monkeypatch.setattr(enr, "fetch_site", _fake_raise)

    profile = await enr.enrich_domain("acme.com", timeout=1.0)
    # DNS survived, so no hard failure.
    assert EnrichmentSource.DNS in profile.sources_ok
    assert EnrichmentSource.WHOIS in profile.sources_failed
    assert EnrichmentSource.SCRAPE in profile.sources_failed
    assert EnrichmentSource.TECH_FINGERPRINT in profile.sources_failed


async def test_orchestrator_all_down_raises(monkeypatch):
    monkeypatch.setattr(enr, "lookup_dns", _fake_raise)
    monkeypatch.setattr(enr, "lookup_whois", _fake_raise)
    monkeypatch.setattr(enr, "fetch_site", _fake_raise)

    with pytest.raises(EnrichmentEmptyError):
        await enr.enrich_domain("acme.com", timeout=1.0)
