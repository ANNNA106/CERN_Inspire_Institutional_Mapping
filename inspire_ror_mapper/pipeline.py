"""
Top-level mapping pipeline: turns a list of raw INSPIRE records into a
scored, decided DataFrame, and flags ROR-id duplicate groups afterward.

Refactor notes (modularization pass)
-------------------------------------
Two things present in the original single-file version were removed here
as confirmed dead code, not behavior changes — flagging both explicitly:

1. A commented-out `already_mapped` / `needs_mapping` block inside
   map_records(). This predates the A0 tier in ror_queries.get_ror_candidates
   (which now re-verifies any pre-existing ROR id as part of the normal
   candidate list, rather than short-circuiting around scoring entirely).
   It was already disabled and superseded; removed rather than carried
   forward as commented-out scaffolding.

2. A `run_pipeline()` convenience wrapper (fetch + map in one call) that
   was never imported or called anywhere — run_pipeline.py's own main()
   does its own fetch/upsert/map sequence inline instead. Confirmed via
   a project-wide grep before removal. If you want this convenience
   function back (e.g. for notebook use), it's a 6-line function —
   happy to re-add it as a thin wrapper around fetch_inspire_records()
   and map_records().
"""

from __future__ import annotations

import logging
import time

import pandas as pd

from .constants import AUTO_ACCEPT_THRESHOLD, REVIEW_THRESHOLD, ROR_QUERY_DELAY, COUNTRY_CODE
from .http_utils import make_session, RateLimitExhausted
from .inspire_client import parse_inspire_record
from .ror_queries import get_ror_candidates
from .scoring import score_candidate, decide

log = logging.getLogger(__name__)


def _no_match_row(inspire: dict) -> dict:
    return {
        "control_number":      inspire["control_number"],
        "legacy_ICN":          inspire["legacy_ICN"],
        "INSPIRE_name":        inspire["official_name"],
        "ROR_id": "", "ROR_name": "",
        "match_score":         0.0,
        "match_method":        "no_candidates",
        "needs_manual_review": True,
        "decision_reason":     "no_ror_candidates_found",
    }


def map_records(
    all_records: list[dict],
    country_filter: str = COUNTRY_CODE,
    auto_accept_threshold: float = AUTO_ACCEPT_THRESHOLD,
    review_threshold: float = REVIEW_THRESHOLD,
    query_delay: float = ROR_QUERY_DELAY,
) -> pd.DataFrame:
    """
    Map a list of raw INSPIRE metadata dicts to ROR identifiers.

    Parameters
    ----------
    all_records           : raw INSPIRE metadata dicts.
    country_filter        : used only for geo-scoring validation, not API filter.
    auto_accept_threshold : composite score above which matches are accepted.
    review_threshold      : composite score below which candidates are dropped.
    query_delay           : seconds between each individual ROR API call.
    """
    session = make_session()
    rows: list[dict] = []
    rate_limit_failures = 0

    log.info("Parsing %d INSPIRE records …", len(all_records))
    parsed = [parse_inspire_record(r) for r in all_records]

    log.info("Querying ROR for %d institutions …", len(parsed))

    for idx, inspire in enumerate(parsed):
        label = inspire["official_name"] or inspire["legacy_ICN"] or str(inspire["control_number"])
        log.info("  [%d/%d]  %s", idx + 1, len(parsed), label)

        try:
            candidates = get_ror_candidates(
                inspire, session, country_filter, query_delay
            )
        except RateLimitExhausted:
            rate_limit_failures += 1
            log.error(
                "  RATE LIMITED on '%s'. Recorded as 'rate_limited'. "
                "Consider increasing query_delay (current: %.2fs).",
                label, query_delay,
            )
            rows.append({
                "control_number":      inspire["control_number"],
                "legacy_ICN":          inspire["legacy_ICN"],
                "INSPIRE_name":        inspire["official_name"],
                "ROR_id": "", "ROR_name": "",
                "match_score":         0.0,
                "match_method":        "rate_limited",
                "needs_manual_review": True,
                "decision_reason":     "rate_limited",
            })
            log.info("  Sleeping 60s to let rate-limit window reset …")
            time.sleep(60)
            continue

        if not candidates:
            log.info("    → no ROR candidates found")
            rows.append(_no_match_row(inspire))
            continue

        scored = sorted(
            [
                (score_candidate(inspire, c, expected_country=country_filter), c)
                for c in candidates
            ],
            key=lambda x: x[0].confidence, reverse=True,
        )

        best_result, best_cand = scored[0]
        second_conf = scored[1][0].confidence if len(scored) > 1 else 0.0

        needs_review, reason = decide(
            best_result, second_conf, auto_accept_threshold, review_threshold,
        )

        log.info(
            "    → conf=%.3f  evid=%.3f  n_sig=%d  veto=%s  method=%-40s  [%s]",
            best_result.confidence, best_result.evidence_score,
            best_result.n_signals, best_result.veto,
            best_result.method, reason,
        )

        has_match = best_result.confidence >= review_threshold
        rows.append({
            "control_number":      inspire["control_number"],
            "legacy_ICN":          inspire["legacy_ICN"],
            "INSPIRE_name":        inspire["official_name"],
            "ROR_id":              best_cand["ror_id"]   if has_match else "",
            "ROR_name":            best_cand["ror_name"] if has_match else "",
            "match_score":         best_result.confidence,
            "evidence_score":      best_result.evidence_score,
            "n_signals":           best_result.n_signals,
            "match_method":        best_result.method,
            "needs_manual_review": needs_review,
            "decision_reason":     reason,
        })

    if rate_limit_failures:
        log.warning(
            "%d institution(s) skipped due to rate limiting. "
            "Rerun with query_delay=3.0 and filter for decision_reason=='rate_limited'.",
            rate_limit_failures,
        )

    return pd.DataFrame(rows).sort_values("match_score", ascending=False).reset_index(drop=True)


def flag_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """Flag INSPIRE records where multiple control_numbers map to one ROR id."""
    mapped = df[df["ROR_id"] != ""].copy()
    group_sizes = mapped.groupby("ROR_id").size()
    df = df.copy()
    df["ror_group_size"]   = df["ROR_id"].map(group_sizes).fillna(0).astype(int)
    df["is_ror_duplicate"] = (df["ror_group_size"] > 1) & (df["ROR_id"] != "")
    return df