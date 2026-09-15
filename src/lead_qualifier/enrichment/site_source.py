"""Single site fetch that feeds both scrape (SiteInfo) and tech fingerprint.

One GET, two derived sources — but each is still marked ok/failed independently
by the orchestrator: a fetch failure fails both scrape and tech together, while
a successful fetch can still yield an empty SiteInfo without affecting tech.
"""

from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

from ..schemas import SiteInfo

_MAX_TEXT = 1500


async def fetch_site(domain: str, client: httpx.AsyncClient) -> httpx.Response:
    # Try https first, fall back to http.
    last_exc: Exception | None = None
    for scheme in ("https", "http"):
        try:
            resp = await client.get(f"{scheme}://{domain}")
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - best-effort, remember last
            last_exc = exc
    raise last_exc if last_exc else RuntimeError("fetch failed")


def parse_site(resp: httpx.Response) -> SiteInfo:
    soup = BeautifulSoup(resp.text, "html.parser")

    title = soup.title.string.strip() if soup.title and soup.title.string else None

    description = None
    tag = soup.find("meta", attrs={"name": "description"})
    if tag and tag.get("content"):
        description = tag["content"].strip()

    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    excerpt = text[:_MAX_TEXT] or None

    return SiteInfo(
        title=title,
        description=description,
        text_excerpt=excerpt,
        status_code=resp.status_code,
    )
