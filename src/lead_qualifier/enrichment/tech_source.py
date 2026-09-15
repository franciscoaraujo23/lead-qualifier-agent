"""Lightweight technology fingerprint from HTTP response headers and HTML markers.

Rule-based and deliberately small (no paid API, no heavy Wappalyzer dependency).
Signal over completeness: a handful of high-confidence markers beats a fragile
exhaustive matcher for a public demo.
"""

from __future__ import annotations

import re

import httpx

# (label, header name, regex over header value)
_HEADER_RULES: list[tuple[str, str, re.Pattern]] = [
    ("nginx", "server", re.compile(r"nginx", re.I)),
    ("Apache", "server", re.compile(r"apache", re.I)),
    ("Cloudflare", "server", re.compile(r"cloudflare", re.I)),
    ("PHP", "x-powered-by", re.compile(r"php", re.I)),
    ("ASP.NET", "x-powered-by", re.compile(r"asp\.net", re.I)),
    ("Express", "x-powered-by", re.compile(r"express", re.I)),
    ("Vercel", "server", re.compile(r"vercel", re.I)),
    ("WordPress", "link", re.compile(r"wp-json", re.I)),
]

# (label, regex over HTML body)
_HTML_RULES: list[tuple[str, re.Pattern]] = [
    ("WordPress", re.compile(r"/wp-content/", re.I)),
    ("Shopify", re.compile(r"cdn\.shopify\.com", re.I)),
    ("React", re.compile(r"__NEXT_DATA__|react(?:-dom)?(?:\.production)?\.min\.js", re.I)),
    ("Next.js", re.compile(r"/_next/static/", re.I)),
    ("Vue", re.compile(r"vue(?:\.runtime)?(?:\.min)?\.js", re.I)),
    ("Google Analytics", re.compile(r"google-analytics\.com|gtag\(", re.I)),
    ("HubSpot", re.compile(r"js\.hs-scripts\.com", re.I)),
    ("Webflow", re.compile(r"assets\.website-files\.com|webflow", re.I)),
]


def fingerprint(resp: httpx.Response) -> list[str]:
    found: set[str] = set()

    for label, header, pattern in _HEADER_RULES:
        value = resp.headers.get(header)
        if value and pattern.search(value):
            found.add(label)

    body = resp.text or ""
    for label, pattern in _HTML_RULES:
        if pattern.search(body):
            found.add(label)

    return sorted(found)
