"""
Low-level HTTP helpers shared by inspire_client.py and ror_queries.py:
session creation, retry-with-backoff, and small URL/domain utilities.

Nothing in this file knows about INSPIRE or ROR response shapes — it's
purely transport-layer.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


class RateLimitExhausted(Exception):
    """Raised when HTTP 429 persists after all retries."""


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "INSPIRE-ROR-Mapper/1.0 (CERN Summer Student; "
            "inspire-feedback@cern.ch)"
        ),
        "Accept": "application/json",
    })
    return s


def get_with_retry(
    session: requests.Session,
    url: str,
    params: dict | None = None,
    max_retries: int = 4,
    base_backoff: float = 2.0,
) -> requests.Response:
    """GET with exponential backoff. Raises RateLimitExhausted on persistent 429."""
    last_was_429 = False
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=20)
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(base_backoff ** attempt)
            continue

        if resp.status_code == 200:
            return resp
        if resp.status_code == 429:
            last_was_429 = True
            wait = float(resp.headers.get("Retry-After", base_backoff ** (attempt + 2)))
            log.warning("HTTP 429; backing off %.1fs …", wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(base_backoff ** attempt)
            continue
        resp.raise_for_status()

    if last_was_429:
        raise RateLimitExhausted(f"429 persisted for {url}")
    raise requests.HTTPError(f"Max retries exceeded for {url}")


def extract_domain(url: str) -> str:
    """Return bare domain (no www., no port). Uses removeprefix to avoid lstrip char-set bug."""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host   = (parsed.netloc or parsed.path).lower().split(":")[0]
        return host.removeprefix("www.")
    except Exception:
        return ""


def domain_overlap(d1: str, d2: str) -> bool:
    """True when one domain is a strict subdomain of the other (e.g. cs.iitb.ac.in ⊂ iitb.ac.in)."""
    d1, d2 = d1.lower(), d2.lower()
    return d1.endswith("." + d2) or d2.endswith("." + d1)


def domain_root_match(d1: str, d2: str) -> bool:
    """
    True when two domains share the same meaningful root label, even across
    different TLDs or ccSLD structures.

    Catches the common Indian institution pattern where INSPIRE has an older
    .org/.com/.net domain while ROR has the canonical .ac.in domain:
      sliet.org  vs  sliet.ac.in   -> root 'sliet'  -> match
      ipr.res.in vs  ipr.ac.in     -> root 'ipr'    -> match
      du.ac.in   vs  du.edu        -> root 'du'     -> match

    Does NOT match:
      nit.ac.in  vs  nitk.ac.in   -> roots 'nit' vs 'nitk' -> no match
      iit.ac.in  vs  iiit.ac.in   -> roots 'iit' vs 'iiit' -> no match

    This is intentionally conservative: only matches when the leftmost
    meaningful label (the institution-specific part) is identical, not just
    similar. We do not use fuzzy matching here because a wrong domain match
    is a worse error than a missed one.

    Known ccSLDs for India that are treated as TLD-like (not institution-specific):
      .ac.in, .edu.in, .res.in, .gov.in, .org.in, .co.in
    """
    def _root_label(domain: str) -> str:
        """Extract the leftmost institution-specific label from a domain."""
        parts = domain.lower().split(".")
        # Strip known multi-part ccSLDs and generic TLDs from the right
        # until we reach the institution-specific part.
        # e.g. ['sliet', 'ac', 'in'] -> strip 'in', strip 'ac' -> 'sliet'
        #      ['sliet', 'org']       -> strip 'org'             -> 'sliet'
        #      ['www', 'sliet', 'ac', 'in'] -> strip right 3, then 'www' -> drop -> 'sliet'
        generic = {"in", "ac", "edu", "res", "gov", "org", "com",
                   "net", "co", "nic", "uk", "au", "cn", "jp", "io"}
        while len(parts) > 1 and parts[-1] in generic:
            parts.pop()
        if len(parts) > 1 and parts[-1] in generic:
            parts.pop()
        # Drop 'www' prefix if present
        if parts and parts[0] == "www":
            parts.pop(0)
        return parts[0] if parts else ""

    r1 = _root_label(d1)
    r2 = _root_label(d2)
    # Must be non-trivial (at least 3 chars) to avoid single-letter false matches
    return bool(r1) and bool(r2) and len(r1) >= 3 and r1 == r2