"""Enrichment orchestrator (§4.9).

Runs every source concurrently and best-effort: a source that fails is recorded
in `sources_failed` and left null. The request only fails (EnrichmentEmptyError)
when *zero* sources produced usable data.
"""

from __future__ import annotations

import asyncio

import httpx

from ..config import settings
from ..errors import EnrichmentEmptyError
from ..schemas import CompanyProfile, EnrichmentSource
from .dns_source import lookup_dns
from .site_source import fetch_site, parse_site
from .tech_source import fingerprint
from .whois_source import lookup_whois

_USER_AGENT = "lead-qualifier/0.1 (+https://github.com/)"


async def enrich_domain(
    domain: str, *, timeout: float | None = None
) -> CompanyProfile:
    timeout = timeout if timeout is not None else settings.enrichment_timeout_s

    ok: list[EnrichmentSource] = []
    failed: list[EnrichmentSource] = []

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        # Each source gets its own wall-clock bound. httpx's timeout is per
        # operation (connect, each read), so a slow-dripping response or a chain
        # of redirects can outlast it. Bounding per source rather than around the
        # whole gather keeps §4.9 intact: a slow source fails alone instead of
        # taking the sources that already answered down with it.
        dns_res, whois_res, site_res = await asyncio.gather(
            asyncio.wait_for(lookup_dns(domain, timeout), timeout),
            asyncio.wait_for(lookup_whois(domain, client), timeout),
            asyncio.wait_for(fetch_site(domain, client), timeout),
            return_exceptions=True,
        )

    # DNS
    dns_info = None
    if isinstance(dns_res, Exception):
        failed.append(EnrichmentSource.DNS)
    else:
        dns_info = dns_res
        ok.append(EnrichmentSource.DNS)

    # WHOIS (RDAP)
    whois_info = None
    if isinstance(whois_res, Exception):
        failed.append(EnrichmentSource.WHOIS)
    else:
        whois_info = whois_res
        ok.append(EnrichmentSource.WHOIS)

    # Site fetch feeds both scrape and tech fingerprint.
    site_info = None
    technologies: list[str] = []
    if isinstance(site_res, Exception):
        failed.append(EnrichmentSource.SCRAPE)
        failed.append(EnrichmentSource.TECH_FINGERPRINT)
    else:
        try:
            site_info = parse_site(site_res)
            ok.append(EnrichmentSource.SCRAPE)
        except Exception:
            failed.append(EnrichmentSource.SCRAPE)
        try:
            technologies = fingerprint(site_res)
            ok.append(EnrichmentSource.TECH_FINGERPRINT)
        except Exception:
            failed.append(EnrichmentSource.TECH_FINGERPRINT)

    if not ok:
        raise EnrichmentEmptyError(
            f"no enrichment source produced usable data for {domain}"
        )

    resolved = dns_info is not None and bool(dns_info.a_records)

    return CompanyProfile(
        domain=domain,
        resolved=resolved,
        dns=dns_info,
        whois=whois_info,
        site=site_info,
        technologies=technologies,
        sources_ok=ok,
        sources_failed=failed,
    )
