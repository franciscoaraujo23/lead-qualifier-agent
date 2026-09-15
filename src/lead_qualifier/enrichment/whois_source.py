"""Registration data via RDAP (the modern, HTTPS+JSON replacement for the WHOIS
protocol). Uses the rdap.org bootstrap redirector so we don't hardcode a
per-TLD server. Redaction is common and treated as a normal state, not failure.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ..schemas import WhoisInfo

_RDAP_BOOTSTRAP = "https://rdap.org/domain/{domain}"


def _parse_event(events: list[dict], action: str) -> datetime | None:
    for ev in events:
        if ev.get("eventAction") == action:
            raw = ev.get("eventDate")
            if raw:
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    return None
    return None


def _parse_registrar(entities: list[dict]) -> tuple[str | None, str | None]:
    registrar = country = None
    for ent in entities:
        roles = ent.get("roles", [])
        vcard = ent.get("vcardArray", [None, []])[1]
        for item in vcard:
            if item[0] == "fn" and "registrar" in roles:
                registrar = item[3]
            if item[0] == "adr" and isinstance(item[3], list) and item[3]:
                country = item[3][-1] or country
    return registrar, country


async def lookup_whois(domain: str, client: httpx.AsyncClient) -> WhoisInfo:
    resp = await client.get(_RDAP_BOOTSTRAP.format(domain=domain))
    resp.raise_for_status()
    data = resp.json()

    events = data.get("events", [])
    entities = data.get("entities", [])
    registrar, country = _parse_registrar(entities)

    # If contact data is entirely absent, the registry is redacting it.
    redacted = registrar is None and not any(
        e.get("roles") for e in entities
    )

    return WhoisInfo(
        registrar=registrar,
        created_on=_parse_event(events, "registration"),
        expires_on=_parse_event(events, "expiration"),
        country=country,
        redacted=redacted,
    )
