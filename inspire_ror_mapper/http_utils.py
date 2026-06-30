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
    d1, d2 = d1.lower(), d2.lower()
    return d1.endswith("." + d2) or d2.endswith("." + d1)