"""
INSPIRE HEP API client: fetching raw institution records and parsing them
into the flat, typed dict shape the rest of the pipeline consumes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from .constants import INSPIRE_API, INSPIRE_FIELDS, INDIAN_STATES, CITY_ALIASES
from .http_utils import make_session, get_with_retry, extract_domain

log = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """
    Normalize institution names for better ROR matching.

    Expands common abbreviations (Inst. → Institute, Tech. → Technology, etc.)
    and normalizes whitespace.
    """
    if not name:
        return ""

    # Normalize all apostrophe/quote Unicode variants to plain ASCII forms.
    # ROR names often use curly apostrophes (' U+2019) while INSPIRE uses
    # straight apostrophes ('). Without this, "St Xavier's College" (INSPIRE)
    # never string-matches "St Xavier's College" (ROR), breaking substring
    # checks, exact-match detection, and fuzzy scores across many institutions
    # with possessive names (St. Xavier's, St. John's, People's, etc.).
    name = name.replace("\u2019", "'").replace("\u2018", "'")
    name = name.replace("\u201c", '"').replace("\u201d", '"')

    # Strip "(India)" suffix from ROR company/branch names — this suffix
    # carries no distinguishing information in an India-only pipeline and
    # prevents "Bharat Petroleum" from matching "Bharat Petroleum (India)".
    # Also strip other country suffixes in parentheses for robustness.
    name = re.sub(r'\s*\(India\)\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s*\([A-Z][a-z]+(?: [A-Z][a-z]+)?\)\s*$', '', name)

    replacements = {
        r"\bInst\.?\b":   "Institute",
        r"\bTech\.?\b":   "Technology",
        r"\bUniv\.?\b":   "University",
        r"\bU\.?\b":      "University",
        r"\bColl\.?\b":   "College",
        r"\bCtr\.?\b":    "Centre",
        r"\bCent\.?\b":   "Centre",
        r"\bAstron\.?\b": "Astronomy",
        r"\bDept\.?\b":   "Department",
        r"\bEng\.?\b":    "Engineering",
        r"\bEngin\.?\b":  "Engineering",
        r"\bNatl\.?\b":   "National",
        r"\bGovt\.?\b":   "Government",
        r"\bGov\.?\b":    "Government",
        r"\bIntl\.?\b":   "International",
        r"\bRes\.?\b":    "Research",
        r"\bSci\.?\b":    "Science",
        r"\bOrg\.?\b":    "Organization",
        
    }

    for pattern, repl in replacements.items():
        name = re.sub(pattern, repl, name, flags=re.IGNORECASE)

    name = re.sub(r"[.,]", " ", name)
    name = " ".join(name.split())

    return name


def fetch_inspire_records(
    country_code: str = "IN",
    page_size: int = 25,
    max_records: int | None = None,
    session: requests.Session | None = None,
) -> list[dict]:
    """Fetch all INSPIRE institution records for a country, handling pagination."""
    if session is None:
        session = make_session()

    records: list[dict] = []
    url = INSPIRE_API
    params: dict[str, Any] = {
        "q":      f"addresses.country_code:{country_code}",
        "fields": INSPIRE_FIELDS,
        "size":   page_size,
        "page":   1,
        "sort":   "mostrecent",
    }

    log.info("Fetching INSPIRE records for country_code=%s …", country_code)

    while True:
        resp = get_with_retry(session, url, params=params)
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            records.append(hit["metadata"])
            if max_records and len(records) >= max_records:
                log.info("Reached max_records=%d, stopping early.", max_records)
                return records
        log.info("  Fetched %d / %d …", len(records), data["hits"].get("total", 0))
        next_url = data.get("links", {}).get("next")
        if not next_url:
            break
        url, params = next_url, {}

    log.info("Fetched %d INSPIRE records total.", len(records))
    return records


def parse_inspire_record(meta: dict) -> dict:
    """Flatten one raw INSPIRE metadata dict into a clean typed dict."""
    ih = meta.get("institution_hierarchy", [])
    official_name = ""
    acronym = ""
    if ih and isinstance(ih[0], dict):
        # Use the LAST (root) entry for the primary name — it's the most
        # queryable and has the standalone ROR identity.
        # The first entry is the most specific sub-unit (department/group).
        root = ih[-1]
        official_name = root.get("name", "") or ""
        acronym       = root.get("acronym", "") or ""
        # If the root is generic (e.g. just "National Institute of Technology"),
        # prefer legacy_ICN which carries the city disambiguator.

    # City/state extracted up front (moved ahead of the legacy_ICN inversion
    # check below, which needs to compare against the city to detect
    # city-leading inversions, not just state-leading ones).
    addrs = meta.get("addresses", [])
    city  = addrs[0].get("cities", [addrs[0].get("city", "")])[0] if addrs else ""
    state = addrs[0].get("state", "") if addrs else ""

    # Some legacy INSPIRE records put a state name into the cities[] field
    # rather than an actual city (e.g. cities=["Meghalaya"] for NIT
    # Meghalaya, whose real city is Shillong). Treating a state name as
    # the city poisons every downstream city-based signal: the affiliation
    # query string, the city-match/city-veto scoring logic, and any
    # subsequent fallback all end up comparing against a state instead of
    # a real place name, which can never agree with ROR's actual city
    # field. Detect this and clear it — better to have no city signal at
    # all than a wrong one masquerading as real data.
    if not state and city.lower() in INDIAN_STATES:
        state = city
        city = ""

    # legacy_ICN is normally "INSTITUTE_NAME, City" (e.g. "ISRO, Bangalore",
    # "BHEL, New Delhi") — institute first, place second. Some records
    # invert this to "PLACE, INSTITUTE_NAME" instead, where PLACE is either
    # a state ("Meghalaya, Natl. Inst. Tech." for NIT Meghalaya) or a city
    # ("Indore, Medi-Caps Inst.", "Bangalore, Nehru Ctr."). Using the raw
    # string as-is in that case produces a garbled, place-leading query
    # that doesn't match ROR's actual naming convention (institute name
    # leading, place trailing or absent) — fuzzy matching degrades badly
    # enough that the correct ROR record may not even be returned, and the
    # postal_name_candidate initialism/substring checks below compare
    # against the wrong segment entirely (the place, not the institute).
    #
    # Detect the inversion two ways and swap segment order before falling
    # back to legacy_ICN:
    #   (a) first comma-segment is a known Indian state name, or
    #   (b) first comma-segment matches this record's own extracted city
    #       (case-insensitive, alias-aware via CITY_ALIASES so "Bangalore"
    #       vs ROR-style "Bengaluru" spelling differences don't block the
    #       detection) — this is the general case state-only detection
    #       missed for "Indore, Medi-Caps Inst." and "Bangalore, Nehru Ctr."
    raw_legacy_icn = (meta.get("legacy_ICN", "") or "").strip()
    legacy_icn_for_name = raw_legacy_icn
    icn_segs = [s.strip() for s in raw_legacy_icn.split(",")]
    if len(icn_segs) >= 2:
        first_seg_lower = icn_segs[0].lower()
        city_lower = city.lower()
        is_state_leading = first_seg_lower in INDIAN_STATES
        is_city_leading = bool(city_lower) and (
            first_seg_lower == city_lower
            or CITY_ALIASES.get(first_seg_lower, first_seg_lower)
               == CITY_ALIASES.get(city_lower, city_lower)
        )
        if is_state_leading or is_city_leading:
            legacy_icn_for_name = ", ".join(icn_segs[1:] + icn_segs[:1])

    # Fallback to legacy_ICN if no official name found
    if not official_name:
        official_name = legacy_icn_for_name

    # Fallback acronym extraction from legacy_ICN when institution_hierarchy
    # is empty/absent (common for older records — e.g. "ISRO, Bangalore").
    # legacy_ICN conventionally leads with "ACRONYM, City[, Dept...]" or
    # "ACRONYM, Dept, City". We match a leading run of 2-10 capital letters
    # in the first comma-segment, rather than requiring the WHOLE segment
    # to be uppercase — "SBMJ Coll., Bangalore" must yield "SBMJ" even
    # though "Coll." (lowercase "oll.") makes the full segment fail an
    # isupper() check. The regex anchors at the start and stops at the
    # first non-capital token, so "Anna University" and "St. Xavier's
    # Coll." correctly yield nothing (they don't start with a capital run).
    # Uses legacy_icn_for_name (post state-inversion-correction) so the
    # acronym is extracted from the institute segment, not a leading
    # state name.
    if not acronym:
        icn_first_seg = legacy_icn_for_name.split(",")[0].strip()
        m = re.match(r"^([A-Z]{2,10})\b", icn_first_seg)
        if m:
            acronym = m.group(1)

    hierarchy_names = [e.get("name") for e in ih if isinstance(e, dict) and e.get("name")]
    name_variants = [v["value"] for v in meta.get("name_variants", []) if v.get("value")]

    # Extract town names from postal_address lines as fallback city signal.
    # INSPIRE sometimes stores district in cities[] but the actual town in
    # postal_address (e.g. cities=["Hooghly"] but postal contains "Serampore").
    _skip = {"india", "in", state.lower(), city.lower()} if state else {"india", "in", city.lower()}
    postal_towns: list[str] = []
    for addr in addrs:
        for line in (addr.get("postal_address") or [])[:4]:
            line = line.strip().strip(",").strip()
            if line and len(line) > 2 and line.lower() not in _skip:
                postal_towns.append(line)

    # The first postal_address line is often the full institution name when
    # INSPIRE's official_name/legacy_ICN is abbreviated (e.g. "Sri Sathya Sai
    # Inst." vs postal "Sri Sathya Sai Institute of Higher Learn[ing]").
    # Heuristic: first line counts as a name candidate if EITHER
    #   (a) it shares its leading tokens with legacy_ICN/official_name
    #       (substring overlap — catches plain abbreviations), or
    #   (b) legacy_ICN's leading token is an *initialism* of the postal
    #       line's words (catches "SBMJ" <- "Sri Bhagawan Mahaveer Jain"),
    #       where the short form's letters never appear as a substring at
    #       all since it's built from first-letters, not a truncation.
    # Both guard against unrelated address fragments being picked up as
    # names by requiring an actual structural relationship to legacy_ICN.
    # Uses legacy_icn_for_name (post state-inversion-correction) so the
    # comparison is against the institute segment, not a leading state.
    postal_name_candidate = ""
    if addrs:
        first_line = (addrs[0].get("postal_address") or [""])[0].strip()
        if first_line:
            icn_first_seg = re.findall(r"[A-Za-z]+", legacy_icn_for_name.split(",")[0])
            icn_lead_token = icn_first_seg[0] if icn_first_seg else ""
            line_lower = first_line.lower()

            # (a) substring overlap on first two ICN words
            icn_words = re.findall(r"[A-Za-z]+", legacy_icn_for_name)[:2]
            substring_match = bool(icn_words) and all(w.lower() in line_lower for w in icn_words)

            # (b) initialism match: icn_lead_token's letters equal the
            # first letters of consecutive words in first_line (allow the
            # initialism to cover a prefix of the line's words, since
            # postal lines often continue with generic suffixes like
            # "College"/"Institute" not reflected in the short form).
            initialism_match = False
            if icn_lead_token and icn_lead_token.isupper() and 2 <= len(icn_lead_token) <= 10:
                line_words = re.findall(r"[A-Za-z]+", first_line)
                if len(line_words) >= len(icn_lead_token):
                    initials = "".join(w[0] for w in line_words[: len(icn_lead_token)]).upper()
                    initialism_match = initials == icn_lead_token.upper()

            if substring_match or initialism_match:
                postal_name_candidate = first_line

    raw_addresses = [
        {
            "postal_code": str(a.get("postal_code", "") or "").strip(),
            "latitude":    a.get("latitude"),
            "longitude":   a.get("longitude"),
            "city": (a.get("cities") or [a.get("city", "")])[0],
            "state":       a.get("state", ""),
        }
        for a in addrs
    ]

    raw_urls = [u["value"] for u in meta.get("urls", []) if u.get("value")]
    domains  = [d for d in (extract_domain(u) for u in raw_urls) if d]

    ext_ids: dict[str, list[str]] = {}
    for ext in meta.get("external_system_identifiers", []):
        schema = ext.get("schema", "").upper()
        value  = ext.get("value", "")
        if schema and value:
            ext_ids.setdefault(schema, []).append(value)

    # Normalise ror field: str | list[dict] | list[str] | dict | None
    existing_ror = None
    for ext in meta.get("external_system_identifiers", []):
        if (ext.get("schema") or "").upper() == "ROR":
            val = ext.get("value", "").strip()
            if val:
                existing_ror = val
                break

    # "ICN" (current/canonical name, distinct from "legacy_ICN") comes back
    # from the API as a list, not a plain string like legacy_ICN — and is
    # frequently a fuller, less-abbreviated name than legacy_ICN. E.g. for
    # control_number 906174: legacy_ICN="Bangalore, Nehru Ctr." (place-
    # leading, institute segment truncated to 2 words) vs.
    # ICN=["Jawaharlal Nehru Ctr for Advanced Sci. Res."] (full name, in
    # correct institute-first order). Take the first entry as a string.
    icn_list = meta.get("ICN") or []
    icn_value = (icn_list[0] if isinstance(icn_list, list) and icn_list else
                 icn_list if isinstance(icn_list, str) else "").strip()

    return {
        "control_number":   meta.get("control_number"),
        "legacy_ICN":       meta.get("legacy_ICN", ""),
        "ICN":              icn_value,
        "official_name":    official_name,
        "acronym":          acronym,
        "hierarchy_names":  hierarchy_names,
        "name_variants":    name_variants,
        "city":             city,
        "state":            state,
        "postal_towns":     postal_towns,
        "postal_name_candidate": postal_name_candidate,
        "domains":          domains,
        "raw_urls":         raw_urls,
        "ext_ids":          ext_ids,
        "existing_ror":     existing_ror,
        "institution_type": meta.get("institution_type", []),
        "_raw_addresses":   raw_addresses,
    }