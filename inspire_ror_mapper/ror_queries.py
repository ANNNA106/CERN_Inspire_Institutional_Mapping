"""
ROR candidate retrieval: every "Axx" query tier that fires against the ROR
API for one INSPIRE record, plus the v2 response parser.

Refactor note (modularization pass)
------------------------------------
The original single-file version had four near-identical inner closures —
one each for: affiliation search, affiliation single_search, structured
domain lookup, and structured exact-name lookup. They differed only in
(a) the request params sent to ROR_AFFIL_API and (b) whether the response
carries a real affiliation score/chosen flag or not. They're collapsed here
into one parameterized `_ror_search()` plus a small per-tier params builder,
with identical dedup (via `seen_ids`), retry, and exception-handling
behavior to the original. No query tier's actual query string or firing
condition was changed — see inspire_client.py / parse_inspire_record's
docstring history (in this package's git history / prior conversation) for
why each tier exists.
"""

from __future__ import annotations

import logging
import time

import requests

from .constants import ROR_AFFIL_API, ROR_QUERY_DELAY
from .http_utils import get_with_retry, extract_domain, RateLimitExhausted
from .inspire_client import normalize_name

log = logging.getLogger(__name__)


def _parse_ror_item(item: dict) -> dict:
    """
    Flatten one ROR v2 record dict.

    Works on both the raw API item format and the nested
    {"organization": {...}, "score": ..., "chosen": ...} format returned
    by the affiliation endpoint (caller unwraps "organization" before calling).
    """
    ror_id = item.get("id", "")

    names_by_type: dict[str, list[str]] = {}
    for n in item.get("names", []):
        for t in n.get("types", ["label"]):
            names_by_type.setdefault(t, []).append(n["value"])

    display_name = (names_by_type.get("ror_display") or [""])[0]
    all_names = (
        names_by_type.get("ror_display", [])
        + names_by_type.get("label",      [])
        + names_by_type.get("alias",      [])
        + names_by_type.get("acronym",    [])
    )
    acronyms = names_by_type.get("acronym", [])

    locs = item.get("locations", [])
    city = country_code = ""
    lat = lng = None
    if locs:
        gd           = locs[0].get("geonames_details", {})
        city         = gd.get("name") or gd.get("city", "")   # v2.1 "name"
        country_code = gd.get("country_code", "")
        try:
            lat = float(gd["lat"])
            lng = float(gd["lng"])
        except (KeyError, TypeError, ValueError):
            pass

    domains = [
        d for d in (
            extract_domain(lnk["value"])
            for lnk in item.get("links", [])
            if lnk.get("type") == "website" and lnk.get("value")
        ) if d
    ]

    ext_ids: dict[str, list[str]] = {}
    for eid in item.get("external_ids", []):
        schema = eid.get("type", "").upper()
        vals   = eid.get("all", [])
        if schema and vals:
            ext_ids[schema] = vals

    return {
        "ror_id":          ror_id,
        "ror_name":        display_name,
        "all_names":       all_names,
        "acronyms":        acronyms,
        "city":            city,
        "country_code":    country_code,
        "domains":         domains,
        "ext_ids":         ext_ids,
        "lat":             lat,
        "lng":             lng,
        "_affil_score":    0.0,   # filled by get_ror_candidates
        "_affil_chosen":   False,
    }


def get_ror_candidates(
    inspire: dict,
    session: requests.Session,
    country_filter: str = "IN",
    query_delay: float = ROR_QUERY_DELAY,
) -> list[dict]:
    """
    Retrieve ROR candidates by firing a sequence of query tiers (A0-A7),
    stopping early in several tiers once a `chosen` result has been found.
    """
    seen_ids: set[str]     = set()
    candidates: list[dict] = []

    # Acronym hint for A5/A6. Prefer the structured acronym (now populated
    # even via the legacy_ICN fallback in parse_inspire_record); only used
    # as a distinct extra query when it isn't already the entirety of
    # official_name/legacy_ICN, since in that case A1 already queried it.
    acronym_hint = (inspire.get("acronym") or "").strip()

    def _build_affil_string(name: str) -> str:
        parts = [name]
        city = (inspire.get("city") or "").strip()
        if city and city.lower() not in name.lower():
            parts.append(city)
        if "india" not in name.lower():
            parts.append("India")
        return ", ".join(parts)

    def _ror_search(params: dict, *, has_affil_score: bool) -> list[dict]:
        """
        Fire one ROR_AFFIL_API request and return parsed, deduped candidates.

        `has_affil_score` controls whether `_affil_score`/`_affil_chosen` are
        taken from the response (affiliation-endpoint queries: A1/A1b/A2b/A3/
        A4/A3b/A5) or defaulted to 0.0/False (structured query.advanced
        lookups with no relevance score: A2/A6).
        """
        time.sleep(query_delay)
        try:
            resp = get_with_retry(session, ROR_AFFIL_API, params=params)
        except RateLimitExhausted:
            raise
        except Exception as exc:
            log.warning("ROR query %s error: %s", params, exc)
            return []

        results = []
        for item in resp.json().get("items", []):
            org    = item.get("organization") or item  # v2 wraps in "organization"
            ror_id = org.get("id", "")
            if not ror_id or ror_id in seen_ids:
                continue
            seen_ids.add(ror_id)
            parsed = _parse_ror_item(org)
            if has_affil_score:
                parsed["_affil_score"]  = item.get("score", 0.0)
                parsed["_affil_chosen"] = item.get("chosen", False)
            else:
                parsed["_affil_score"]  = 0.0
                parsed["_affil_chosen"] = False
            results.append(parsed)
        return results

    def _affil(q: str) -> list[dict]:
        """Affiliation NLP search."""
        q = (q or "").strip()
        if len(q) < 2:
            return []
        return _ror_search({"affiliation": q}, has_affil_score=True)

    def _affil_single(q: str) -> list[dict]:
        """Affiliation NLP search, single_search mode (returns ROR's single best guess)."""
        q = (q or "").strip()
        if len(q) < 2:
            return []
        return _ror_search({"affiliation": q, "single_search": ""}, has_affil_score=True)

    def _query_domain(domain: str) -> list[dict]:
        """Structured exact match against ROR's `domains` field."""
        domain = (domain or "").strip()
        if not domain:
            return []
        return _ror_search({"query.advanced": f"domains:{domain}"}, has_affil_score=False)

    def _query_exact_name(token: str) -> list[dict]:
        """
        Structured exact match against ROR's `names.value` field — NOT the
        affiliation NLP endpoint.

        The affiliation endpoint scores by fuzzy similarity over full
        label/alias text, so a bare acronym like "BHEL" or "ISRO" gets
        diluted against thousands of longer, generically-similar org
        names and can fail to surface even when the ROR record has that
        exact acronym as a structured name (types=["acronym"]).
        query.advanced against names.value does a literal field match
        instead of fuzzy NLP scoring, so it isn't subject to the same
        dilution — it either matches the token or it doesn't.
        """
        token = (token or "").strip()
        if not token:
            return []
        return _ror_search({"query.advanced": f'names.value:"{token}"'}, has_affil_score=False)

    # A0: inject pre-existing ROR ID as a candidate (before all API calls).
    # This ensures records already mapped in INSPIRE are re-verified rather
    # than silently trusted or silently ignored.
    existing_ror = inspire.get("existing_ror")
    if existing_ror:
        try:
            time.sleep(query_delay)
            # Convert ROR website URL to API endpoint
            # "https://ror.org/04kf25f32" → "https://api.ror.org/v2/organizations/04kf25f32"
            ror_bare_id = existing_ror.rstrip("/").split("/")[-1]
            api_url = f"https://api.ror.org/v2/organizations/{ror_bare_id}"
            resp = get_with_retry(session, api_url)
            org = resp.json()
            parsed_existing = _parse_ror_item(org)
            parsed_existing["_affil_score"] = 0.0
            parsed_existing["_affil_chosen"] = False
            parsed_existing["_query_source"] = "A0:existing_ror"
            if parsed_existing["ror_id"] not in seen_ids:
                seen_ids.add(parsed_existing["ror_id"])
                candidates.append(parsed_existing)
        except Exception as exc:
            log.warning("Failed to fetch existing ROR %s: %s", existing_ror, exc)

    # A1: legacy_ICN + city + country
    icn_raw = (inspire.get("legacy_ICN") or "").strip()
    if icn_raw:
        batch = _affil(_build_affil_string(icn_raw))
        for c in batch:
            c["_query_source"] = f"A1:ICN({_build_affil_string(icn_raw)})"
        candidates.extend(batch)

    # A1b: modern ICN field + city + country.
    #
    # "ICN" (current canonical name) is a separate field from "legacy_ICN"
    # (older, often heavily abbreviated/truncated short form) and is
    # frequently a much fuller name. E.g. control_number 906174:
    #   legacy_ICN = "Bangalore, Nehru Ctr."          (place-leading, institute
    #                                                   segment truncated to 2 words)
    #   ICN        = "Jawaharlal Nehru Ctr for Advanced Sci. Res."
    #                                                  (full name, correct order)
    # Querying ICN in addition to legacy_ICN gives a second, independent
    # shot at a usable name string whenever legacy_ICN is too mangled or
    # truncated for the affiliation matcher to work with — this costs one
    # extra API call but only when ICN is present and differs from
    # legacy_ICN, so it's cheap relative to the recall it can recover.
    icn_modern = (inspire.get("ICN") or "").strip()
    if icn_modern and icn_modern.lower() != icn_raw.lower():
        batch = _affil(_build_affil_string(normalize_name(icn_modern)))
        for c in batch:
            c["_query_source"] = f"A1b:ICN_modern({_build_affil_string(normalize_name(icn_modern))})"
        candidates.extend(batch)

    # A2: domain lookup (catches renamed institutions like VIT)
    domains = inspire.get("domains", [])
    domain_query_hit = False
    if domains:
        for domain in domains[:2]:  # try at most 2 domains
            batch = _query_domain(domain)
            if batch:
                domain_query_hit = True
            for c in batch:
                c["_query_source"] = f"A2:domain({domain})"
            candidates.extend(batch)

    # A2b: domain via the affiliation endpoint, as a fallback when the
    # structured query.advanced "domains:" field search (A2) returns
    # nothing.
    #
    # ROR's structured `domains` array is curated separately from the
    # `links` field and is frequently empty even when an org clearly has
    # a website — both BHEL (ror.org/03ky3pc21) and JNCASR
    # (ror.org/0538gdx71) have "domains": [] despite having real websites
    # under `links`. A2's query.advanced=domains:X search legitimately
    # returns zero results in that case — it isn't a bug in our query, the
    # ROR field itself is empty.
    #
    # The affiliation endpoint's NLP matcher generally still recognizes a
    # bare domain string and can surface the org via its `links`-derived
    # website text, so we retry there as a fallback rather than guessing
    # at an unconfirmed query.advanced field path for `links`.
    if domains and not domain_query_hit:
        for domain in domains[:2]:
            batch = _affil(domain)
            for c in batch:
                c["_query_source"] = f"A2b:domain_affil({domain})"
            candidates.extend(batch)

    # A3: official name + city + country
    if len(candidates) < 3:
        q3 = _build_affil_string(normalize_name(inspire["official_name"]))
        batch = _affil(q3)
        for c in batch:
            c["_query_source"] = f"A3:name({q3})"
        candidates.extend(batch)

    # A4: single_search fallback if no chosen result yet
    if not any(c.get("_affil_chosen") for c in candidates):
        q4 = _build_affil_string(normalize_name(inspire["official_name"]))
        batch = _affil_single(q4)
        for c in batch:
            c["_query_source"] = f"A4:single({q4})"
        candidates.extend(batch)

    # A3b: name_variants query.
    # Fires when official_name is just an acronym/abbreviation (e.g. "DHWU, India")
    # but name_variants contains the full name ROR actually uses
    # (e.g. "Diamond Harbour Women's University").
    # Runs after A4 so it only fires when earlier attempts found no chosen result.
    if not any(c.get("_affil_chosen") for c in candidates):
        official_norm = normalize_name(inspire["official_name"]).lower()
        for variant in inspire.get("name_variants", [])[:2]:
            if not variant:
                continue
            variant_norm = normalize_name(variant).lower()
            # Only query if variant is meaningfully different from official_name
            if variant_norm == official_norm:
                continue
            qv = _build_affil_string(variant)
            batch = _affil(qv)
            for c in batch:
                c["_query_source"] = f"A3b:variant({variant})"
            candidates.extend(batch)

    # A5: acronym-only + country query.
    # Fires when nothing has been chosen yet AND the INSPIRE name we've been
    # querying with is itself short/acronym-like (e.g. legacy_ICN="ISRO,
    # Bangalore" -> official_name="ISRO, Bangalore"). In that situation A1/A3
    # are *the same query string*, diluted by city + generic words, and ROR's
    # affiliation matcher has nothing distinctive to lock onto — a 4-letter
    # acronym competing against full institute names loses to fuzzy noise.
    # Querying the acronym alone (without the city) lets ROR's NLP affiliation
    # matcher weight the acronym field directly instead of diluting it.
    # We deliberately do NOT append city here: for national bodies (ISRO,
    # ISRO Satellite Centre, DRDO, BARC, etc.) the seat city is frequently
    # *not* the ROR-registered headquarters city, and including it can
    # actively suppress the correct match.
    if not any(c.get("_affil_chosen") for c in candidates) and acronym_hint:
        q5 = f"{acronym_hint}, India"
        batch = _affil(q5)
        for c in batch:
            c["_query_source"] = f"A5:acronym({q5})"
        candidates.extend(batch)

    # A6: exact structured-name field match for the acronym, bypassing the
    # affiliation NLP matcher entirely.
    #
    # Why this is needed even after A5: the affiliation endpoint (used by
    # A1/A3/A4/A5) is a *similarity-ranked* fuzzy matcher over label/alias
    # text. A bare 4-letter acronym like "BHEL" or "ISRO" can still fail
    # to surface there even alone with "India", because the endpoint is
    # ranking against full-text similarity across its whole index, not
    # doing an exact lookup on the structured acronym field. BHEL's ROR
    # record has names=[{"types":["acronym"],"value":"BHEL"}, ...] — an
    # exact match if we query that field directly via query.advanced
    # instead of asking the NLP matcher to rank it among fuzzy neighbors.
    #
    # Fires only if A5 still found nothing chosen — this is a strictly
    # narrower, more literal query than A5 and should be a last resort
    # before falling through to manual review.
    if not any(c.get("_affil_chosen") for c in candidates) and acronym_hint:
        batch = _query_exact_name(acronym_hint)
        for c in batch:
            c["_query_source"] = f"A6:exact_name({acronym_hint})"
        candidates.extend(batch)

    # A7: postal_name_candidate affiliation query.
    #
    # Fires when nothing chosen yet and INSPIRE's postal_address first line
    # looks like the institute's real full name (already detected and
    # stashed by parse_inspire_record as postal_name_candidate).
    #
    # This catches *initialism* acronyms — where legacy_ICN's short form is
    # built from the first letters of the full name's words, e.g. "SBMJ"
    # from "Sri Bhagawan Mahaveer Jain [College]" — which neither A1 (fuzzy
    # match on the abbreviated legacy_ICN string) nor A5/A6 (acronym-field
    # queries) can solve: "SBMJ" never appears as a structured acronym
    # name on the ROR side at all, because ROR only stores the expansion
    # ("Jain University" / "Sri Bhagawan Mahaveer Jain College"), not the
    # initialism INSPIRE happens to abbreviate it to. Querying the postal
    # full name directly is the only path that can surface this record.
    #
    # We deliberately query with single_search (not city-augmented) since
    # the postal line is already a full institutional name as opposed to
    # a bare token — adding city here mainly risks dilution the way it
    # did for A1, with no compensating benefit.
    postal_name_hint = (inspire.get("postal_name_candidate") or "").strip()
    if not any(c.get("_affil_chosen") for c in candidates) and postal_name_hint:
        q7 = postal_name_hint
        if "india" not in q7.lower():
            q7 = f"{q7}, India"
        batch = _affil_single(q7)
        for c in batch:
            c["_query_source"] = f"A7:postal_name({q7})"
        candidates.extend(batch)

    return candidates