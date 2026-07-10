"""
Reporting and debugging utilities: run summaries, CSV exports for human
review, and ad-hoc single-record inspection tools.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .http_utils import make_session
from .inspire_client import parse_inspire_record
from .ror_queries import get_ror_candidates
from .scoring import score_candidate
from .constants import COUNTRY_CODE

log = logging.getLogger(__name__)


def print_summary(df: pd.DataFrame) -> None:
    total         = len(df)
    pre_existing  = (df["decision_reason"] == "pre_existing").sum()
    auto_accepted = (df["decision_reason"] == "auto_accepted").sum()
    needs_review  = df["needs_manual_review"].sum()
    no_candidates = (df["decision_reason"] == "no_ror_candidates_found").sum()
    rate_limited  = (df["decision_reason"] == "rate_limited").sum()
    vetoed        = df["decision_reason"].str.startswith("vetoed:").sum()

    print("\n" + "=" * 62)
    print("  INSPIRE → ROR  |  Run Summary")
    print("=" * 62)
    print(f"  Total records processed   : {total}")
    print(f"  Already mapped (skip)     : {pre_existing}")
    print(f"  Auto-accepted             : {auto_accepted}")
    print(f"  Flagged for manual review : {needs_review}")
    print(f"    of which: vetoed        : {vetoed}  ← contradictory evidence")
    print(f"    of which: no candidates : {no_candidates}")
    if rate_limited:
        print(f"    of which: rate-limited  : {rate_limited}  ← rerun these")
    print("=" * 62)

    new = df[~df["decision_reason"].isin(
        ["pre_existing", "no_ror_candidates_found", "rate_limited"]
    )].copy()
    if not new.empty:
        score_col = "match_score"  # this is now confidence
        print("\n  Confidence distribution (newly mapped):")
        bins   = [0, 0.5, 0.7, 0.87, 1.001]
        labels = ["<0.50 (discard)", "0.50–0.70 (low)", "0.70–0.87 (medium)", "≥0.87 (auto)"]
        new["score_bin"] = pd.cut(new[score_col], bins=bins, labels=labels, right=False)
        for label, count in new["score_bin"].value_counts().sort_index().items():
            print(f"    {label:32s}: {count}")

        # Show signal diversity distribution
        if "n_signals" in new.columns:
            print("\n  Signal diversity (# independent signals that fired):")
            for n, count in new["n_signals"].value_counts().sort_index().items():
                print(f"    {n} signals : {count}")
    print()


def export_for_manual_review(
    df: pd.DataFrame,
    path: str = "review_queue.csv",
    *,
    also_write_tiers: bool = True,
) -> None:
    """
    Write all records that need manual review to ``path``.

    The old implementation filtered ``ROR_id != ""``, which silently dropped
    every low-confidence and no-candidate record — the majority of the review
    queue.  This version includes ALL rows where ``needs_manual_review=True``,
    regardless of whether a candidate ROR id was found.

    A ``review_tier`` column is added to help reviewers prioritise:

      tier 1 – vetoed        : pipeline detected a contradiction (e.g. country
                               mismatch, affil_chosen + name mismatch).  A ROR
                               candidate was found but should NOT be trusted.
                               Action: discard the candidate and search manually.

      tier 2 – has_candidate : confidence is in the medium range (0.50–0.87) or
                               the record was flagged high_conf_ambiguous.  A ROR
                               candidate exists and is plausible but not certain.
                               Action: verify the suggested ROR_id.

      tier 3 – no_candidate  : pipeline found no usable candidate (no_ror_
                               candidates_found, low_confidence, rate_limited).
                               ROR_id is empty.
                               Action: search ROR manually and fill in.

    If ``also_write_tiers=True`` (default), three separate tier CSV files are
    also written alongside ``path`` (e.g. review_queue_tier1_vetoed.csv).
    This avoids one 571-row flat file that is hard to work through.
    """
    cols = [
        "control_number", "legacy_ICN", "INSPIRE_name",
        "ROR_id", "ROR_name", "match_score", "evidence_score",
        "n_signals", "match_method", "decision_reason",
    ]
    cols = [c for c in cols if c in df.columns]

    # All rows that need manual review — no ROR_id filter.
    subset = df[df["needs_manual_review"]].copy()

    # Assign tiers.
    def _tier(row: pd.Series) -> int:
        reason = str(row.get("decision_reason", ""))
        if reason.startswith("vetoed:"):
            return 1
        if str(row.get("ROR_id", "")).strip():
            return 2
        return 3

    subset["review_tier"] = subset.apply(_tier, axis=1)

    # Sort: tier ascending (1 first), then confidence descending within tier.
    subset = subset.sort_values(
        ["review_tier", "match_score"], ascending=[True, False]
    )

    # Write the combined queue.
    out_cols = ["review_tier"] + [c for c in cols if c != "review_tier"]
    subset[out_cols].to_csv(path, index=False)

    n1 = (subset["review_tier"] == 1).sum()
    n2 = (subset["review_tier"] == 2).sum()
    n3 = (subset["review_tier"] == 3).sum()
    log.info(
        "Manual review queue → %s  (%d rows: %d vetoed, %d has-candidate, %d no-candidate)",
        path, len(subset), n1, n2, n3,
    )

    if also_write_tiers:
        stem   = Path(path).stem
        suffix = Path(path).suffix
        parent = Path(path).parent

        tier_meta = [
            (1, "vetoed",        "contradiction detected — discard candidate and search manually"),
            (2, "has_candidate", "plausible candidate — verify the suggested ROR_id"),
            (3, "no_candidate",  "no candidate found — search ROR manually"),
        ]
        for tier_n, tier_name, _ in tier_meta:
            tier_df   = subset[subset["review_tier"] == tier_n][out_cols]
            tier_path = parent / f"{stem}_tier{tier_n}_{tier_name}{suffix}"
            tier_df.to_csv(tier_path, index=False)
            log.info("  tier %d → %s  (%d rows)", tier_n, tier_path, len(tier_df))


def export_duplicate_groups(
    df: pd.DataFrame,
    path: str = "ror_duplicates.csv",
) -> None:
    """
    Write a grouped view of every ROR id that is shared by more than one
    INSPIRE record, so you can verify whether the group really is one
    organisation or a misclustering.

    Output format
    -------------
    The CSV has a ``row_type`` column:

      "group_header"  — one row per ROR id; shows the shared ROR name,
                        total member count, and the range of match scores
                        across members.  ROR_id / ROR_name are filled in;
                        all INSPIRE-specific columns are blank.

      "member"        — one row per INSPIRE record in the group, sorted by
                        descending match_score within the group.  All
                        columns are filled in normally.

    Groups are sorted by descending member count (largest groups first),
    then alphabetically by ROR_name, so the most ambiguous clusters appear
    at the top.

    Example
    -------
    row_type      , ROR_id              , ROR_name              , group_size , INSPIRE_name          , match_score , …
    group_header  , https://ror.org/xxx , IIT Bombay            , 3          ,                       ,             ,
    member        , https://ror.org/xxx , IIT Bombay            ,            , Indian Inst. Tech. B. , 0.94        ,
    member        , https://ror.org/xxx , IIT Bombay            ,            , IIT Bombay Dept Phys  , 0.88        ,
    member        , https://ror.org/xxx , IIT Bombay            ,            , I.I.T. Bombay         , 0.85        ,
    group_header  , …
    """
    dup_df = df[df["is_ror_duplicate"]].copy() if "is_ror_duplicate" in df.columns \
             else df[df["ROR_id"] != ""].copy()

    if dup_df.empty:
        log.info("No ROR duplicate groups to export.")
        return

    # Member columns we care about (keep only those present in the DataFrame).
    member_cols = [
        "control_number", "legacy_ICN", "INSPIRE_name",
        "match_score", "evidence_score", "n_signals",
        "match_method", "decision_reason",
    ]
    member_cols = [c for c in member_cols if c in df.columns]

    output_rows: list[dict] = []

    # Sort members once: by ROR_id (to keep groups together), then descending score.
    members_sorted = dup_df.sort_values(
        ["ROR_id", "match_score"], ascending=[True, False]
    )

    # Build groups sorted by descending size then ROR name.
    group_meta = (
        dup_df.groupby("ROR_id")
        .agg(
            ROR_name   = ("ROR_name",    "first"),
            group_size = ("ROR_id",      "count"),
            score_min  = ("match_score", "min"),
            score_max  = ("match_score", "max"),
        )
        .reset_index()
        .sort_values(["group_size", "ROR_name"], ascending=[False, True])
    )

    for _, meta in group_meta.iterrows():
        ror_id   = meta["ROR_id"]
        ror_name = meta["ROR_name"]
        size     = int(meta["group_size"])
        s_min    = round(float(meta["score_min"]), 3)
        s_max    = round(float(meta["score_max"]), 3)

        # ── Group header row ──────────────────────────────────────────────
        output_rows.append({
            "row_type":    "group_header",
            "ROR_id":      ror_id,
            "ROR_name":    ror_name,
            "group_size":  size,
            "score_range": f"{s_min}–{s_max}",
            **{c: "" for c in member_cols},
        })

        # ── Member rows ───────────────────────────────────────────────────
        members = members_sorted[members_sorted["ROR_id"] == ror_id]
        for _, row in members.iterrows():
            output_rows.append({
                "row_type":    "member",
                "ROR_id":      ror_id,
                "ROR_name":    ror_name,
                "group_size":  "",
                "score_range": "",
                **{c: row[c] for c in member_cols},
            })

    out_df = pd.DataFrame(output_rows)
    # Put structural columns first, then member detail columns.
    front = ["row_type", "ROR_id", "ROR_name", "group_size", "score_range"]
    out_df = out_df[front + [c for c in member_cols if c in out_df.columns]]
    out_df.to_csv(path, index=False)

    n_groups = len(group_meta)
    n_members = len(dup_df)
    log.info(
        "Duplicate groups → %s  (%d groups, %d total INSPIRE records)",
        path, n_groups, n_members,
    )


def debug_candidates(
    control_number: int,
    records_cache: str = "inspire_records_cache.json",
    country_filter: str = COUNTRY_CODE,
    query_delay: float = 1.6,
) -> None:
    """
    Print all ROR candidates retrieved for a single INSPIRE record,
    with full per-candidate scoring breakdown.

    Usage:
        from inspire_ror_mapper import debug_candidates
        debug_candidates(1267123)
    """
    with Path(records_cache).open() as f:
        raw_records = json.load(f)

    # Find the target record
    target_meta = next(
        (r for r in raw_records if r.get("control_number") == control_number),
        None,
    )
    if target_meta is None:
        print(f"control_number {control_number} not found in cache.")
        return

    inspire = parse_inspire_record(target_meta)
    print(f"\n{'='*70}")
    print(f"  INSPIRE record: {control_number}  —  {inspire['official_name']}")
    print(f"  legacy_ICN:     {inspire['legacy_ICN']}")
    print(f"  city:           {inspire['city']}")
    print(f"  domains:        {inspire['domains']}")
    print(f"  ext_ids:        {inspire['ext_ids']}")
    print(f"{'='*70}\n")

    session = make_session()
    candidates = get_ror_candidates(inspire, session, country_filter, query_delay)

    if not candidates:
        print("  No ROR candidates returned.\n")
        return

    print(f"  {len(candidates)} candidate(s) returned:\n")
    header = f"  {'ROR ID':<30} {'ROR name':<45} {'city':<18} {'affil':>6} {'chosen':>6}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    scored = sorted(
        [(score_candidate(inspire, c, expected_country=country_filter), c) for c in candidates],
        key=lambda x: x[0].confidence, reverse=True,
    )

    for result, cand in scored:
        print(
            f"  {cand['ror_id']:<30} {cand['ror_name']:<45} "
            f"{cand['city']:<18} {cand['_affil_score']:>6.3f} {str(cand['_affil_chosen']):>6}"
        )
        print(
            f"    conf={result.confidence:.4f}  evid={result.evidence_score:.4f}  "
            f"n_sig={result.n_signals}  veto={result.veto}  method={result.method}"
        )
        print(f"    source={cand.get('_query_source', 'unknown')}")
        print()


def print_inspire_record(control_number: int, records_cache: str = "inspire_records_cache.json") -> None:
    """Pretty-print the raw INSPIRE JSON for a single control number."""
    with Path(records_cache).open() as f:
        raw_records = json.load(f)

    record = next((r for r in raw_records if r.get("control_number") == control_number), None)

    if record is None:
        print(f"control_number {control_number} not found in cache.")
        return

    print(json.dumps(record, indent=2))