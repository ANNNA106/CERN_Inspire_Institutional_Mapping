import json
import logging
from pathlib import Path

import pandas as pd

from db_manager import (
    init_db, upsert_institutions, save_mapping_run,
    fetch_paper_counts, get_summary,
)
from inspire_ror_mapper import (
    fetch_inspire_records,
    parse_inspire_record,
    flag_duplicates,
    map_records,
    print_summary,
    export_for_manual_review,
    export_duplicate_groups,
    debug_candidates,
    print_inspire_record,
)

# CONFIGURATION
COUNTRY_CODE          = "IN"
MAX_RECORDS           = None
AUTO_ACCEPT_THRESHOLD = 0.85
REVIEW_THRESHOLD      = 0.50
ROR_API_DELAY         = 0.12
PIPELINE_VERSION      = "2.0"   # bump this whenever you change scoring logic

OUTPUT_ALL        = "inspire_ror_all.csv"
OUTPUT_REVIEW     = "review_queue.csv"
OUTPUT_AUTO       = "auto_accepted.csv"
OUTPUT_DUPLICATES = "ror_duplicates.csv"
RECORDS_CACHE     = "inspire_records_cache.json"

log = logging.getLogger(__name__)


def load_or_fetch_records() -> list[dict]:
    cache = Path(RECORDS_CACHE)
    if cache.exists():
        log.info("Loading records from cache: %s", cache)
        with cache.open() as f:
            return json.load(f)

    records = fetch_inspire_records(country_code=COUNTRY_CODE, max_records=MAX_RECORDS)
    with cache.open("w") as f:
        json.dump(records, f)
    log.info("Records cached to %s", cache)
    return records


def _compute_review_tiers(df: pd.DataFrame) -> dict[int, int]:
    """
    Same tiering logic export_for_manual_review() uses internally, pulled
    out so the tier can also be written into mapping_runs and queried in
    SQL — not just visible in the review_queue CSV.

      tier 1 - vetoed        : contradiction detected, discard & search manually
      tier 2 - has_candidate : plausible candidate, verify it
      tier 3 - no_candidate  : nothing found, search ROR manually
    """
    tiers: dict[int, int] = {}
    for _, row in df[df["needs_manual_review"]].iterrows():
        reason = str(row.get("decision_reason", ""))
        if reason.startswith("vetoed:"):
            tier = 1
        elif str(row.get("ROR_id", "")).strip():
            tier = 2
        else:
            tier = 3
        tiers[int(row["control_number"])] = tier
    return tiers


def main() -> None:
    init_db()

    all_records = load_or_fetch_records()

    # Parse once, reuse for both DB identity upsert and mapping
    parsed = [parse_inspire_record(r) for r in all_records]
    upsert_institutions(parsed)

    # Map to ROR
    df = map_records(
        all_records,
        country_filter=COUNTRY_CODE,
        auto_accept_threshold=AUTO_ACCEPT_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        query_delay=ROR_API_DELAY,
    )

    df = flag_duplicates(df)
    print_summary(df)

    # CSV outputs (unchanged — useful for spreadsheet review)
    df.to_csv(OUTPUT_ALL, index=False)
    print(f"Full results         -> {OUTPUT_ALL}  ({len(df)} rows)")

    auto_df = df[df["decision_reason"] == "auto_accepted"]
    auto_df.to_csv(OUTPUT_AUTO, index=False)
    print(f"Auto-accepted        -> {OUTPUT_AUTO}  ({len(auto_df)} rows)")

    export_for_manual_review(df, OUTPUT_REVIEW)
    print(f"Review queue         -> {OUTPUT_REVIEW}")

    dup_df = df[df["is_ror_duplicate"]] if "is_ror_duplicate" in df.columns else df.iloc[0:0]
    if not dup_df.empty:
        export_duplicate_groups(df, OUTPUT_DUPLICATES)
        n_groups = dup_df["ROR_id"].nunique()
        print(f"ROR duplicate groups -> {OUTPUT_DUPLICATES}  ({n_groups} groups, {len(dup_df)} INSPIRE records)")
    else:
        print("No ROR duplicates found.")

    # Database write — history-preserving, with review tiers attached
    review_tiers = _compute_review_tiers(df)
    run_id = save_mapping_run(df, pipeline_version=PIPELINE_VERSION, review_tiers=review_tiers)
    print(f"Mapping run saved     -> run_id={run_id}")

    # Paper counts for anything a human will need to look at: the full
    # manual-review set (vetoed + has-candidate + no-candidate), not just
    # rows with an empty ROR_id. A vetoed candidate still carries a
    # ROR_id (the rejected one) so curators can see what was rejected —
    # that previously made it look "mapped" to this filter and excluded
    # it from the paper-count fetch, even though paper count is exactly
    # the prioritization signal a curator needs for vetoed records too.
    review_cns = df[df["needs_manual_review"]]["control_number"].tolist()
    if review_cns:
        print(f"\nFetching paper counts for {len(review_cns)} institutions needing manual review...")
        fetch_paper_counts(review_cns)

    get_summary()


if __name__ == "__main__":
    main()