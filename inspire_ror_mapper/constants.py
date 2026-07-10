"""
Module-level constants for the INSPIRE → ROR mapping pipeline.
"""

from __future__ import annotations


# ROR API
ROR_AFFIL_API = "https://api.ror.org/v2/organizations"   # ?affiliation=

# Seconds between individual ROR API calls (40 req/min limit → 1.6 s/call safe)
ROR_QUERY_DELAY = 0.2


# INSPIRE API
INSPIRE_API = "https://inspirehep.net/api/institutions"

INSPIRE_FIELDS = ",".join([
    "control_number", "legacy_ICN", "ICN",
    "institution_hierarchy", "addresses", "urls",
    "institution_type", "name_variants",
    "external_system_identifiers", "ror",
])

COUNTRY_CODE = "IN"   

# Scoring weights
WEIGHTS = {
    "name":        0.25,
    "affiliation": 0.25,
    "domain":      0.20,
    "ext_id":      0.20,
    "location":    0.07,
    "acronym":     0.03,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

# Number of independent signal categories (for diversity bonus).
N_SIGNAL_CATEGORIES = len(WEIGHTS)   # 6


# ---------------------------------------------------------------------------
# Decision thresholds
# ---------------------------------------------------------------------------
# decide() receives the full ScoringResult for the best candidate and the
# raw confidence of the runner-up, instead of two bare floats. This allows
# veto conditions detected inside score_candidate to flow through to the
# decision without the two functions sharing mutable state.
#
# Thresholds (all applied to ``confidence``, not ``evidence_score``):
#
#   AUTO_ACCEPT_THRESHOLD  0.87  — slightly above the old 0.85, but because
#     confidence now discounts single-signal results via the diversity bonus,
#     this is effectively *more permissive* for multi-signal matches and
#     *more conservative* for single-signal matches. Net effect: the IIT
#     Hyderabad profile (ext_id+domain+affil, no name) auto-accepts; a single
#     affil_chosen without corroboration does not.
#
#   REVIEW_THRESHOLD  0.50  — unchanged. Records below this have too little
#     evidence to present a useful candidate to a human reviewer.
#
#   GAP_THRESHOLD  0.04  — reduced from 0.05. The diversity bonus already
#     separates well-evidenced matches from near-ties; the hard gap check can
#     be slightly looser without re-introducing ambiguity.
# ---------------------------------------------------------------------------

AUTO_ACCEPT_THRESHOLD = 0.87
REVIEW_THRESHOLD      = 0.50
GAP_THRESHOLD          = 0.04


# ---------------------------------------------------------------------------
# Reference data: generic institute names, Indian states, city aliases
# ---------------------------------------------------------------------------

# name_exact + acronym + strong affil: all three naming signals agree.
GENERIC_NAMES = {
    "national institute of technology",
    "indian institute of technology",
    "indian institute of management",
    "indian institute of science education and research",
    "university of delhi",
    "regional engineering college",
    "engineering college",
    "college of engineering",
    "government college",
    "institute of technology",
    "st xaviers college",
    "st xavier's college",
}

INDIAN_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram",
    "nagaland", "odisha", "orissa", "punjab", "rajasthan", "sikkim", "tamil nadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal",
    "delhi", "jammu and kashmir", "ladakh",
}

# Canonical city name aliases: old name → new name, and common aliases.
# Used to prevent the city-veto logic in scoring.py from vetoing valid
# matches where INSPIRE uses an old city name or a known alias.
#
# NOTE (left as-is from the original file, flagging for visibility only):
# "shimla"/"simla" map to each other rather than both to one canonical
# spelling. This is harmless for the current lookup pattern (either side
# of the alias still resolves to the other), but is inconsistent with
# every other entry in this dict, which all converge on one canonical
# spelling. Not changed here since it doesn't affect behavior — flagging
# in case you want it tidied up later.
CITY_ALIASES: dict[str, str] = {
    # Renamed cities
    "allahabad":    "prayagraj",
    "prayagraj":    "prayagraj",
    "bombay":       "mumbai",
    "calcutta":     "kolkata",
    "madras":       "chennai",
    "bangalore":    "bengaluru",
    "bengaluru":    "bengaluru",
    "baroda":       "vadodara",
    "poona":        "pune",
    "cuttack":      "bhubaneswar",   # sometimes used interchangeably
    # Known aliases (same area)
    "cochin":       "ernakulam",
    "kochi":        "ernakulam",
    "ernakulam":    "ernakulam",
    "burdwan":      "bardhaman",
    "bardhaman":    "bardhaman",
    "trivandrum":   "thiruvananthapuram",
    "thiruvananthapuram": "thiruvananthapuram",
    "mysore":       "mysuru",
    "mysuru":       "mysuru",
    "hubli":        "hubballi",
    "hubballi":     "hubballi",
    "shimla":       "simla",
    "simla":        "shimla",
    "barasat":      "kolkata",
    "baranagar":    "kolkata",
    "barrackpore":  "kolkata",
    "dum dum":      "kolkata",
    "howrah":       "kolkata",
    "salt lake":    "kolkata",
    "rajarhat":     "kolkata",
    "behala":       "kolkata",
    "garia":        "kolkata",
    "kalol":        "ahmedabad",
    # Campus towns that ROR geocodes to a nearby district/district HQ
    "longowal":     "sangrur",       # SLIET campus in Longowal, Sangrur district
    "bhat":         "gandhinagar",   # IPR campus in Bhat township, Gandhinagar district
}