"""DNS lookup. Synchronous dnspython run in a thread so it fits the async,
per-source-timeout model of the orchestrator.
"""

from __future__ import annotations

import asyncio

import dns.resolver

from ..schemas import DnsInfo

_RECORD_TYPES = ("A", "MX", "NS")


def _resolve_sync(domain: str, timeout: float) -> DnsInfo:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout

    def _query(rtype: str) -> list[str]:
        try:
            answers = resolver.resolve(domain, rtype)
            return [r.to_text() for r in answers]
        except Exception:
            # A record type may be absent (e.g. no MX) without the domain being
            # unresolvable; caller decides "resolved" from the A records.
            return []

    return DnsInfo(
        a_records=_query("A"),
        mx_records=_query("MX"),
        ns_records=_query("NS"),
    )


async def lookup_dns(domain: str, timeout: float) -> DnsInfo:
    info = await asyncio.to_thread(_resolve_sync, domain, timeout)
    if not (info.a_records or info.ns_records):
        # No A and no NS => the domain does not resolve at all.
        raise LookupError(f"{domain} does not resolve")
    return info
