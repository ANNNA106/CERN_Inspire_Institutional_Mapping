"""
bulk_confirm_auto_accepted.py
================================
Marks high-confidence auto-accepted institutions as 'confirmed' in the
curation table, in bulk, based on a match_score threshold AND a hard cap
on row count.

This is the ONLY thing that makes a record eligible for push_to_inspire.py
(via v_ready_for_pushback) — the pipeline scoring alone never pushes
anything. This script is the deliberate human decision point.

Usage:
    python bulk_confirm_auto_accepted.py --min-score 0.92 --limit 10
        # prints what WOULD be confirmed, does not write to curation table

    python bulk_confirm_auto_accepted.py --min-score 0.92 --limit 10 --commit
        # actually calls mark_curation() for each matching row (max 10)
"""
import argparse

import pandas as pd

from db_manager import get_conn, mark_curation


def get_auto_accepted_above(min_score: float, limit: int | None = None) -> pd.DataFrame:
    conn = get_conn()
    sql = """
        SELECT control_number, official_name, legacy_ICN, city,
               ROR_id, ROR_name, match_score, match_method
        FROM v_auto_accepted
        WHERE match_score >= ?
        ORDER BY match_score DESC
    """
    params = [min_score]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def already_curated(control_numbers: list[int]) -> set[int]:
    """control_numbers that already have ANY curation row (any status) —
    so a rerun doesn't re-curate (and doesn't overwrite a status someone
    may have deliberately set differently, e.g. after a manual review)."""
    if not control_numbers:
        return set()
    conn = get_conn()
    placeholders = ",".join("?" * len(control_numbers))
    rows = conn.execute(
        f"SELECT control_number FROM curation WHERE control_number IN ({placeholders})",
        control_numbers,
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def main():
    parser = argparse.ArgumentParser(description="Bulk-confirm high-confidence auto-accepted mappings")
    parser.add_argument("--min-score", type=float, required=True,
                         help="Only confirm rows with match_score >= this value")
    parser.add_argument("--limit", type=int, default=10,
                         help="Hard cap on how many NEW rows get confirmed in this run "
                              "(default 10). Applied after excluding already-curated rows, "
                              "so you always get up to this many *new* confirmations.")
    parser.add_argument("--commit", action="store_true",
                         help="Actually write to the curation table. Without this, only previews.")
    parser.add_argument("--curated-by", type=str, default="ananya")
    args = parser.parse_args()

    # Fetch generously above the threshold (no limit yet) so the later cap
    # is applied to *new* rows only, not to whatever happens to be already
    # curated among the top N by score.
    df = get_auto_accepted_above(args.min_score, limit=None)
    if df.empty:
        print(f"No auto-accepted rows with match_score >= {args.min_score}.")
        return

    already = already_curated(df["control_number"].tolist())
    to_curate_all = df[~df["control_number"].isin(already)]
    skipped = df[df["control_number"].isin(already)]
    to_curate = to_curate_all.head(args.limit)
    capped_out = len(to_curate_all) - len(to_curate)

    print(f"Auto-accepted with match_score >= {args.min_score}: {len(df)} rows total")
    print(f"  Already curated (any status, skipped here)        : {len(skipped)}")
    print(f"  New, eligible                                      : {len(to_curate_all)}")
    print(f"  Capped at --limit {args.limit}                      -> {len(to_curate)} will be confirmed this run")
    if capped_out > 0:
        print(f"  ({capped_out} more are eligible but held back by --limit — rerun to confirm more)")
    print()

    if to_curate.empty:
        print("Nothing new to curate.")
        return

    print(to_curate[["control_number", "official_name", "ROR_id", "match_score", "match_method"]]
          .to_string(index=False))
    print()

    if not args.commit:
        print(f"[PREVIEW] {len(to_curate)} rows would be marked 'confirmed'. "
              f"Re-run with --commit to actually write to the curation table.")
        return

    notes = f"bulk-confirmed: auto_accepted, match_score >= {args.min_score}"
    n = 0
    for _, row in to_curate.iterrows():
        mark_curation(
            control_number=int(row["control_number"]),
            status="confirmed",
            curated_by=args.curated_by,
            notes=notes,
        )
        n += 1

    print(f"\nDone. {n} institutions marked 'confirmed'.")
    print("Next: check `python -c \"from db_manager import get_conn; import pandas as pd; "
          "print(pd.read_sql('SELECT * FROM v_ready_for_pushback', get_conn()))\"` "
          "to see what's now eligible for pushback.")


if __name__ == "__main__":
    main()