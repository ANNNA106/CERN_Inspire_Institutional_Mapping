"""
INSPIRE HEP → ROR Mapping Pipeline
====================================
Maps Indian INSPIRE institution records (without a ROR identifier) to their
correct ROR organization.

Architecture: two-stage matching
---------------------------------
Stage 1 – ROR Affiliation API  (?affiliation=)
  The ROR affiliation endpoint is purpose-built for exactly this task: given
  a free-text institution name it returns ranked candidates with an NLP-based
  confidence score and a boolean ``chosen`` flag (ROR's own recommendation).
  We use this as the primary retrieval + pre-scoring step.

Stage 2 – Multi-signal re-scoring
  We re-score every ROR candidate with five independent signals (name, domain,
  external-id, location, acronym) to produce a final composite score that is
  robust to cases where the affiliation API's NLP ranking diverges from
  ground-truth signals like GRID/Wikidata IDs and website domains.

Why NOT ?query= + country filter
---------------------------------
The previous implementation used ?query= with a country filter. This failed
in two separate ways:
  1. For generic names ("Indian Institute of Technology"), ?query= returns
     20 Indian institutions with the same words, all scoring ~0.9 on
     token_set_ratio — so every IIT got matched to every other IIT.
  2. For abbreviated names ("TIFR", "BARC"), ?query= with a strict country
     filter sometimes returns 0 results because the filter was rejecting
     candidates whose ROR record had slightly different location metadata.

The affiliation API avoids both problems: it uses Elasticsearch NLP, not
token overlap, and does not require an explicit country filter.

Package layout
---------------
This used to be one ~2000-line file. It's now split by responsibility:

  constants.py        thresholds, weights, reference data (states/cities/etc)
  http_utils.py        session/retry/domain-extraction helpers (no INSPIRE
                       or ROR-specific knowledge)
  inspire_client.py    fetch + parse INSPIRE records, name normalization
  ror_queries.py        get_ror_candidates() — the Axx query-tier waterfall
  scoring.py            score_candidate(), decide()
  pipeline.py          map_records(), flag_duplicates() — orchestrates the
                       above into a DataFrame
  reporting.py          CSV exports, run summaries, single-record debug tools

Every name below is re-exported at the package's top level, so existing
code that does ``from inspire_ror_mapper import X`` keeps working unchanged.
"""

from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

from .constants import (
    ROR_AFFIL_API,
    ROR_QUERY_DELAY,
    INSPIRE_API,
    INSPIRE_FIELDS,
    WEIGHTS,
    N_SIGNAL_CATEGORIES,
    AUTO_ACCEPT_THRESHOLD,
    REVIEW_THRESHOLD,
    GAP_THRESHOLD,
    GENERIC_NAMES,
    INDIAN_STATES,
    CITY_ALIASES,
)
from .http_utils import (
    RateLimitExhausted,
    make_session,
    get_with_retry,
    extract_domain,
    domain_overlap,
)
from .inspire_client import (
    normalize_name,
    fetch_inspire_records,
    parse_inspire_record,
)
from .geo_scoring import (
    GeoScorer,
)
from .ror_queries import (
    get_ror_candidates,
)
from .scoring import (
    ScoringResult,
    score_candidate,
    decide,
)
from .pipeline import (
    map_records,
    flag_duplicates,
)
from .reporting import (
    print_summary,
    export_for_manual_review,
    export_duplicate_groups,
    debug_candidates,
    print_inspire_record,
)

__all__ = [
    # constants
    "ROR_AFFIL_API", "ROR_QUERY_DELAY", "INSPIRE_API", "INSPIRE_FIELDS",
    "WEIGHTS", "N_SIGNAL_CATEGORIES",
    "AUTO_ACCEPT_THRESHOLD", "REVIEW_THRESHOLD", "GAP_THRESHOLD",
    "GENERIC_NAMES", "INDIAN_STATES", "CITY_ALIASES",
    # http
    "RateLimitExhausted", "make_session", "get_with_retry",
    "extract_domain", "domain_overlap",
    # inspire
    "normalize_name", "fetch_inspire_records", "parse_inspire_record",
    # ror
    "get_ror_candidates",
    # scoring
    "ScoringResult", "score_candidate", "decide",
    # pipeline
    "map_records", "flag_duplicates",
    # reporting
    "print_summary", "export_for_manual_review", "export_duplicate_groups",
    "debug_candidates", "print_inspire_record",
]