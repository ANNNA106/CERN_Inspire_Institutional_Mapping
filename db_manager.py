# db_manager.py
"""
Schema
------
institutions   one row per INSPIRE control_number — identity data, plus
                paper_count. Overwritten wholesale on every fetch/refresh
                — it's just "what does INSPIRE currently say about this
                institution" (name, city, paper count, etc). Merged from
                two tables in an earlier version: paper_count used to live
                in its own `paper_counts` table, but it's written by the
                same actor (the pipeline, on its own refresh schedule) and
                read the same way as every other institution fact, so the
                split didn't earn its complexity — it's one row per
                control_number either way.

mapping_runs    one row per (control_number, run_id) — full pipeline output,
                history-preserving. Every pipeline run appends new rows
                instead of overwriting, tagged with run_id/run_at. This is
                what lets you answer "did this flip between runs, and why."
                Kept separate from institutions: this is 1:many per
                institution (multiple runs over time), not 1:1, so it
                can't be folded in without losing history.

curation        one row per control_number — the human-verified decision.
                This is the ONLY table the write-back script reads from.
                Never touched by the pipeline, only by mark_curation().
                Kept separate from institutions: different writer
                (human, not the automated pipeline refresh) and merging
                it in risks a pipeline rerun silently overwriting a human
                decision if any column were ever missed from an UPDATE's
                exclusion list — this happened once already in an earlier
                single-table design (review_tier never reaching the DB).

pushback_log    one row per write-back attempt against inspirebeta.net.
                Tracks what's actually been pushed, so reruns don't
                double-write and you always know what's left to do.
                Kept separate: 1:many per institution (one row per
                attempt — retries, failures, successes all logged), not
                1:1, so it can't be folded into institutions or curation
                without losing the attempt history.

Views (always derived from the latest run, never stored/duplicated)
---------------------------------------------------------------------
v_latest_run        latest mapping_runs row per control_number
v_auto_accepted      v_latest_run WHERE decision_reason = 'auto_accepted'
v_manual_review      v_latest_run WHERE needs_manual_review = 1
v_ready_for_pushback curated rows not yet successfully pushed
"""
import sqlite3
import uuid
import pandas as pd
from datetime import datetime, timezone

DB_PATH = "inspire_ror.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 1.  SCHEMA
def init_db() -> None:
    """Create tables/views if they don't exist. Safe to call on every run."""
    conn = get_conn()
    conn.executescript("""
        -- Identity data for every Indian INSPIRE institution record, plus
        -- paper count. Refreshed wholesale each time you re-fetch from
        -- INSPIRE / re-run fetch_paper_counts.
        CREATE TABLE IF NOT EXISTS institutions (
            control_number          INTEGER PRIMARY KEY,
            legacy_ICN              TEXT,
            official_name           TEXT,
            acronym                 TEXT,
            city                    TEXT,
            state                   TEXT,
            country_code            TEXT DEFAULT 'IN',
            existing_ror            TEXT,    -- ROR id INSPIRE already had, if any
            institution_type       TEXT,    -- JSON-encoded list
            last_fetched_at         TEXT,
            paper_count             INTEGER,
            paper_count_fetched_at  TEXT
        );

        -- One row per pipeline run per institution. Never UPDATEd, only
        -- INSERTed — this is the history table.
        CREATE TABLE IF NOT EXISTS mapping_runs (
            run_id              TEXT NOT NULL,
            run_at              TEXT NOT NULL,
            pipeline_version    TEXT,
            control_number      INTEGER NOT NULL,
            ROR_id              TEXT,
            ROR_name            TEXT,
            match_score         REAL,
            evidence_score      REAL,
            n_signals           INTEGER,
            match_method        TEXT,
            decision_reason     TEXT,
            needs_manual_review INTEGER,
            review_tier         INTEGER,   -- 1=vetoed 2=has_candidate 3=no_candidate, NULL if not in review
            is_ror_duplicate    INTEGER DEFAULT 0,
            ror_group_size      INTEGER,
            PRIMARY KEY (run_id, control_number),
            FOREIGN KEY (control_number) REFERENCES institutions(control_number)
        );
        CREATE INDEX IF NOT EXISTS idx_runs_cn      ON mapping_runs(control_number);
        CREATE INDEX IF NOT EXISTS idx_runs_run_at  ON mapping_runs(run_at);
        CREATE INDEX IF NOT EXISTS idx_runs_reason  ON mapping_runs(decision_reason);

        -- Paper counts now live directly on `institutions` (merged from a
        -- separate paper_counts table — both were 1:1 per control_number,
        -- refreshed by the same overall process, so the split wasn't
        -- earning its complexity).

        -- Human curation decisions. The pipeline never writes here.
        CREATE TABLE IF NOT EXISTS curation (
            control_number   INTEGER PRIMARY KEY,
            curation_status  TEXT,   -- confirmed | corrected | no_ror_exists | skip_low_priority
            curated_ror_id   TEXT,
            curated_by       TEXT,
            curated_at       TEXT,
            curation_notes   TEXT,
            FOREIGN KEY (control_number) REFERENCES institutions(control_number)
        );

        -- Audit trail for the GET-modify-PUT write-back step against
        -- inspirebeta.net. One row per attempt (success or failure), so a
        -- crash mid-run can be resumed without double-writing.
        CREATE TABLE IF NOT EXISTS pushback_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            control_number   INTEGER NOT NULL,
            ror_id_written   TEXT,
            ror_id_replaced  TEXT,   -- previous ROR value, if this write overwrote one
            attempted_at     TEXT NOT NULL,
            http_status      INTEGER,
            success          INTEGER NOT NULL DEFAULT 0,
            etag_used        TEXT,
            error_message    TEXT,
            dry_run          INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (control_number) REFERENCES institutions(control_number)
        );
        CREATE INDEX IF NOT EXISTS idx_pushback_cn ON pushback_log(control_number);

        -- Latest run per institution (the table everything else views off of)
        DROP VIEW IF EXISTS v_latest_run;
        CREATE VIEW v_latest_run AS
        SELECT mr.*
        FROM mapping_runs mr
        WHERE mr.run_at = (
            SELECT MAX(mr2.run_at) FROM mapping_runs mr2
            WHERE mr2.control_number = mr.control_number
        );

        DROP VIEW IF EXISTS v_auto_accepted;
        CREATE VIEW v_auto_accepted AS
        SELECT i.control_number, i.legacy_ICN, i.official_name, i.city, i.state,
               lr.ROR_id, lr.ROR_name, lr.match_score, lr.evidence_score,
               lr.n_signals, lr.match_method, lr.run_id, lr.run_at
        FROM v_latest_run lr
        JOIN institutions i ON i.control_number = lr.control_number
        WHERE lr.decision_reason = 'auto_accepted';

        DROP VIEW IF EXISTS v_manual_review;
        CREATE VIEW v_manual_review AS
        SELECT i.control_number, i.legacy_ICN, i.official_name, i.city, i.state,
               lr.ROR_id, lr.ROR_name, lr.match_score, lr.evidence_score,
               lr.n_signals, lr.match_method, lr.decision_reason, lr.review_tier,
               i.paper_count, c.curation_status
        FROM v_latest_run lr
        JOIN institutions i ON i.control_number = lr.control_number
        LEFT JOIN curation c ON c.control_number = i.control_number
        WHERE lr.needs_manual_review = 1;

        -- Curated + not-yet-successfully-pushed: what the write-back script
        -- should actually act on. A row drops out once pushback_log has a
        -- success=1 row with the same ror_id_written for it.
        DROP VIEW IF EXISTS v_ready_for_pushback;
        CREATE VIEW v_ready_for_pushback AS
        SELECT c.control_number, i.legacy_ICN, i.official_name,
               COALESCE(c.curated_ror_id, lr.ROR_id) AS ror_id_to_write,
               c.curation_status, c.curated_by, c.curated_at
        FROM curation c
        JOIN institutions i ON i.control_number = c.control_number
        LEFT JOIN v_latest_run lr ON lr.control_number = c.control_number
        WHERE c.curation_status IN ('confirmed', 'corrected')
          AND COALESCE(c.curated_ror_id, lr.ROR_id, '') != ''
          AND c.control_number NOT IN (
              SELECT p.control_number FROM pushback_log p
              WHERE p.success = 1 AND p.dry_run = 0
                AND p.ror_id_written = COALESCE(c.curated_ror_id, lr.ROR_id)
          );
    """)
    conn.commit()
    conn.close()
    print(f"Database initialised -> {DB_PATH}")


# 2.  WRITES FROM THE PIPELINE
def upsert_institutions(parsed_records: list[dict]) -> None:
    """
    Refresh identity data from a list of parse_inspire_record() outputs.
    Call this once per pipeline run, before save_mapping_run().
    """
    import json as _json
    conn = get_conn()
    now = _now()
    for p in parsed_records:
        conn.execute("""
            INSERT INTO institutions (
                control_number, legacy_ICN, official_name, acronym,
                city, state, country_code, existing_ror,
                institution_type, last_fetched_at
            ) VALUES (
                :control_number, :legacy_ICN, :official_name, :acronym,
                :city, :state, 'IN', :existing_ror,
                :institution_type, :now
            )
            ON CONFLICT(control_number) DO UPDATE SET
                legacy_ICN       = excluded.legacy_ICN,
                official_name    = excluded.official_name,
                acronym          = excluded.acronym,
                city             = excluded.city,
                state            = excluded.state,
                existing_ror     = excluded.existing_ror,
                institution_type = excluded.institution_type,
                last_fetched_at  = excluded.last_fetched_at
        """, {
            "control_number":   int(p["control_number"]),
            "legacy_ICN":       p.get("legacy_ICN", ""),
            "official_name":    p.get("official_name", ""),
            "acronym":          p.get("acronym", ""),
            "city":             p.get("city", ""),
            "state":            p.get("state", ""),
            "existing_ror":     p.get("existing_ror") or "",
            "institution_type": _json.dumps(p.get("institution_type", [])),
            "now":              now,
        })
    conn.commit()
    conn.close()
    print(f"Upserted {len(parsed_records)} institution identity rows")


def save_mapping_run(df: pd.DataFrame, pipeline_version: str = "1.0",
                      review_tiers: dict[int, int] | None = None) -> str:
    """
    Append one full pipeline run to mapping_runs. Returns the run_id.

    review_tiers : optional {control_number: tier} map, e.g. built from
                   export_for_manual_review()'s tier logic, so the tier is
                   queryable in SQL instead of only living in a CSV.
    """
    conn = get_conn()
    run_id = str(uuid.uuid4())
    run_at = _now()
    review_tiers = review_tiers or {}

    for _, row in df.iterrows():
        r = row.to_dict()
        cn = int(r["control_number"])
        conn.execute("""
            INSERT INTO mapping_runs (
                run_id, run_at, pipeline_version, control_number,
                ROR_id, ROR_name, match_score, evidence_score,
                n_signals, match_method, decision_reason,
                needs_manual_review, review_tier,
                is_ror_duplicate, ror_group_size
            ) VALUES (
                :run_id, :run_at, :pipeline_version, :control_number,
                :ROR_id, :ROR_name, :match_score, :evidence_score,
                :n_signals, :match_method, :decision_reason,
                :needs_manual_review, :review_tier,
                :is_ror_duplicate, :ror_group_size
            )
        """, {
            "run_id":              run_id,
            "run_at":              run_at,
            "pipeline_version":    pipeline_version,
            "control_number":      cn,
            "ROR_id":              r.get("ROR_id", ""),
            "ROR_name":            r.get("ROR_name", ""),
            "match_score":         r.get("match_score"),
            "evidence_score":      r.get("evidence_score"),
            "n_signals":           r.get("n_signals"),
            "match_method":        r.get("match_method", ""),
            "decision_reason":     r.get("decision_reason", ""),
            "needs_manual_review": int(bool(r.get("needs_manual_review", False))),
            "review_tier":         review_tiers.get(cn),
            "is_ror_duplicate":    int(bool(r.get("is_ror_duplicate", False))),
            "ror_group_size":      r.get("ror_group_size"),
        })

    conn.commit()
    conn.close()
    print(f"Saved mapping run {run_id} -> {len(df)} rows")
    return run_id


def fetch_paper_counts(control_numbers: list[int]) -> None:
    """Fetch paper counts directly from INSPIRE institution records."""
    import requests
    import time

    conn = get_conn()
    now = _now()
    updated = 0

    for cn in control_numbers:
        url = f"https://inspirehep.net/api/institutions/{cn}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 429:
                print("Rate limited, waiting 10 seconds...")
                time.sleep(10)
                resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            count = data.get("metadata", {}).get("number_of_papers", 0)

            conn.execute("""
                UPDATE institutions
                SET paper_count = ?,
                    paper_count_fetched_at = ?
                WHERE control_number = ?
            """, (count, now, cn))

            updated += 1
            if updated % 50 == 0:
                conn.commit()
                print(f"Progress: {updated}/{len(control_numbers)}")
            time.sleep(0.2)

        except Exception as e:
            print(f"Failed for {cn}: {e}")

    conn.commit()
    conn.close()
    print(f"Paper counts updated for {updated}/{len(control_numbers)} institutions.")


# 3.  CURATION
def mark_curation(
    control_number: int,
    status: str,
    curated_ror_id: str | None = None,
    notes: str | None = None,
    curated_by: str = "ananya",
) -> None:
    """
    Record a manual curation decision for one institution.

    status options:
      'confirmed'         - pipeline mapping is correct, verified by human
      'corrected'         - pipeline was wrong, curated_ror_id has the fix
      'no_ror_exists'     - checked ROR, no record exists for this institution
      'skip_low_priority' - too few papers, not worth manual lookup
    """
    valid = {"confirmed", "corrected", "no_ror_exists", "skip_low_priority"}
    if status not in valid:
        raise ValueError(f"status must be one of {valid}, got {status!r}")
    if status == "corrected" and not curated_ror_id:
        raise ValueError("status='corrected' requires curated_ror_id")

    conn = get_conn()
    conn.execute("""
        INSERT INTO curation (control_number, curation_status, curated_ror_id,
                               curated_by, curated_at, curation_notes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(control_number) DO UPDATE SET
            curation_status = excluded.curation_status,
            curated_ror_id  = excluded.curated_ror_id,
            curated_by      = excluded.curated_by,
            curated_at      = excluded.curated_at,
            curation_notes  = excluded.curation_notes
    """, (control_number, status, curated_ror_id, curated_by, _now(), notes))
    conn.commit()
    conn.close()
    print(f"Curation saved for {control_number}: {status}")


def unmark_curation(control_number: int, force: bool = False) -> None:
    """
    Remove a curation decision entirely (deletes the row from `curation`),
    pulling it back OUT of v_ready_for_pushback if it's still pending.

    If this control_number has already been SUCCESSFULLY pushed live
    (pushback_log has a success=1, dry_run=0 row for it), this refuses by
    default — un-curating it locally doesn't undo the write that already
    happened on inspirebeta.net, so silently deleting the curation record
    would erase the audit trail explaining why that write happened. Pass
    force=True to delete anyway if you really mean to (e.g. you're about
    to push a corrected value and want a clean slate).
    """
    conn = get_conn()
    pushed = conn.execute("""
        SELECT COUNT(*) FROM pushback_log
        WHERE control_number = ? AND success = 1 AND dry_run = 0
    """, (control_number,)).fetchone()[0]

    if pushed and not force:
        conn.close()
        print(f"control_number {control_number} was already successfully pushed live "
              f"({pushed} record(s) in pushback_log). Refusing to delete its curation "
              f"row without force=True, since that would remove the audit trail for a "
              f"write that already happened on inspirebeta.net. If you intend to "
              f"re-curate it (e.g. with a corrected ROR id), call mark_curation() again "
              f"instead — it will overwrite in place. Use force=True only if you really "
              f"want to delete the curation row outright.")
        return

    cur = conn.execute("DELETE FROM curation WHERE control_number = ?", (control_number,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    if deleted:
        print(f"Curation row removed for {control_number} — no longer in v_ready_for_pushback "
              f"(unless it was already pushed and excluded for that reason).")
    else:
        print(f"No curation row existed for {control_number} — nothing to remove.")


# 4.  PUSHBACK LOG
def log_pushback(control_number: int, ror_id_written: str, success: bool,
                  http_status: int | None = None, etag_used: str | None = None,
                  error_message: str | None = None, dry_run: bool = True,
                  ror_id_replaced: str | None = None) -> None:
    conn = get_conn()
    conn.execute("""
        INSERT INTO pushback_log
            (control_number, ror_id_written, ror_id_replaced, attempted_at,
             http_status, success, etag_used, error_message, dry_run)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (control_number, ror_id_written, ror_id_replaced, _now(), http_status,
          int(success), etag_used, error_message, int(dry_run)))
    conn.commit()
    conn.close()


# 5.  READ HELPERS
def query_unmapped_by_priority() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql("""
        SELECT i.control_number, i.legacy_ICN, i.official_name, i.city,
               lr.review_tier, lr.match_method, i.paper_count, c.curation_status
        FROM institutions i
        LEFT JOIN v_latest_run lr ON lr.control_number = i.control_number
        LEFT JOIN curation c       ON c.control_number = i.control_number
        WHERE (lr.ROR_id IS NULL OR lr.ROR_id = '')
          AND (c.curated_ror_id IS NULL OR c.curated_ror_id = '')
          AND (c.curation_status IS NULL OR c.curation_status != 'skip_low_priority')
        ORDER BY i.paper_count DESC
    """, conn)
    conn.close()
    return df


def get_summary() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM institutions")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM v_latest_run WHERE ROR_id IS NOT NULL AND ROR_id != ''")
    mapped = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM v_auto_accepted")
    auto = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM v_manual_review")
    review = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM curation")
    curated = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM v_ready_for_pushback")
    ready = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT run_id) FROM mapping_runs")
    n_runs = cur.fetchone()[0]

    conn.close()
    print(f"""
Database summary ({DB_PATH}):
  Total institutions        : {total}
  Pipeline runs recorded     : {n_runs}
  Mapped (latest run)        : {mapped}
  Auto-accepted (latest)     : {auto}
  In manual review (latest)  : {review}
  Manually curated           : {curated}
  Ready for pushback         : {ready}
    """)