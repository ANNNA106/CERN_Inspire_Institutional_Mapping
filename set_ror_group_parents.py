"""
set_ror_group_parents.py
=========================
For each ROR-duplicate group in `ror_duplicates.csv`, designate one
auto-accepted INSPIRE record as the "parent" and write a
related_records / relation=parent link on every OTHER auto-accepted
record in that group (the children), pointing at the parent.

Leader/parent assignment is NOT inferred here — you supply it explicitly
via a leaders file (see load_group_leaders()). This avoids accidentally
picking the wrong "main" record out of a group like main-university vs.
department, which only a human should decide.

Only auto_accepted members (decision_reason == "auto_accepted" in
ror_duplicates.csv) are ever included — vetoed and medium-confidence
members are never linked.

Contract for the INSPIRE write-back (confirmed from the browser network
tab against inspirebeta.net):

    GET  /api/institutions/{control_number}      -> read current record + ETag
    PUT  /api/institutions/{control_number}       -> full flat metadata body
         Headers: If-Match: <ETag from GET response, including W/"..." form>
                  Content-Type: application/json
                  Authorization: Bearer <token>

    Body is the metadata at TOP LEVEL (no {"metadata": {...}} wrapper).
    Server-computed / read-only fields must be stripped before PUT:
        - self, $schema  (self is server-managed; re-sending it is harmless
          but $schema should be preserved as-is, NOT stripped — INSPIRE
          requires it on PUT. Only strip true server-computed fields.)
        - metadata.ror (legacy computed field, if present)
        - number_of_papers
        - addresses[].country (server-derived from country_code)
    related_records is a normal editable field — we just append/replace
    the parent entry in it.

Modes
-----
DRY RUN (default): fetches each child record, shows you the exact diff
    to related_records and the exact PUT body that WOULD be sent. No
    network writes. No DB writes. Always do this before --live.

LIVE (--live): after typed confirmation, performs the GET-modify-PUT
    for real, and logs each attempt to the project's existing
    `pushback_log` table in inspire_ror.db (created with this schema
    if the table doesn't exist yet; if it already exists with a
    different schema, this script uses the columns that already match
    and will fail loudly rather than silently inventing new ones — see
    ensure_pushback_log_table()).

Usage
-----
    # 1. Dry run — always do this first
    python set_ror_group_parents.py --duplicates ror_duplicates.csv \\
        --leaders group_leaders.json

    # 2. Once the printed diffs look right, actually push:
    python set_ror_group_parents.py --duplicates ror_duplicates.csv \\
        --leaders group_leaders.json --live

group_leaders.json / .csv format
---------------------------------
JSON:  { "<ROR_id>": <parent_control_number>, ... }
   or:  { "<ROR_id>": "<parent_control_number>", ... }

CSV:   columns "ROR_id,parent_control_number"

You only need to list groups where you've chosen a leader. Groups not
listed are skipped (printed as skipped, not an error) — same for groups
with fewer than 2 auto_accepted members (nothing to link).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("set_ror_group_parents")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

INSPIRE_API = "https://inspirebeta.net/api/institutions"
DB_PATH_DEFAULT = "inspire_ror.db"

# Fields INSPIRE computes server-side; never send these back on PUT.
# (NOTE: '$schema' and 'self' are intentionally NOT in this list — the
# working PUT captured from the network tab included both, and omitting
# '$schema' causes INSPIRE to reject the record.)
SERVER_COMPUTED_TOP_LEVEL = {
    "number_of_papers",
    "legacy_version",  # server bumps this; sending stale value is harmless
                        # but we still strip+let server set it to be safe
}


# ---------------------------------------------------------------------------
# 1. Load group leaders (the "who is the parent" decision — supplied by you)
# ---------------------------------------------------------------------------
def load_group_leaders(path: str) -> dict[str, int]:
    """
    Load {ROR_id: parent_control_number} from a JSON or CSV file.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Group leaders file not found: {path}")

    if p.suffix.lower() == ".json":
        raw = json.loads(p.read_text())
        return {str(k): int(v) for k, v in raw.items()}

    if p.suffix.lower() == ".csv":
        leaders: dict[str, int] = {}
        with p.open(newline="") as f:
            for row in csv.DictReader(f):
                leaders[str(row["ROR_id"]).strip()] = int(row["parent_control_number"])
        return leaders

    raise ValueError(f"Unsupported leaders file type: {p.suffix} (use .json or .csv)")


# ---------------------------------------------------------------------------
# 2. Load ror_duplicates.csv and build auto-accepted groups
# ---------------------------------------------------------------------------
@dataclass
class GroupMember:
    control_number: int
    legacy_ICN: str
    inspire_name: str
    decision_reason: str


@dataclass
class DupGroup:
    ror_id: str
    ror_name: str
    members: list[GroupMember] = field(default_factory=list)

    @property
    def auto_accepted(self) -> list[GroupMember]:
        return [m for m in self.members if m.decision_reason == "auto_accepted"]


def load_duplicate_groups(path: str) -> dict[str, DupGroup]:
    groups: dict[str, DupGroup] = {}
    current_ror: str | None = None

    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["row_type"] == "group_header":
                current_ror = row["ROR_id"]
                groups[current_ror] = DupGroup(ror_id=current_ror, ror_name=row["ROR_name"])
            elif row["row_type"] == "member":
                ror = row["ROR_id"]
                if ror not in groups:
                    # defensive: a member row appeared without its header
                    groups[ror] = DupGroup(ror_id=ror, ror_name=row.get("ROR_name", ""))
                groups[ror].members.append(
                    GroupMember(
                        control_number=int(row["control_number"]),
                        legacy_ICN=row.get("legacy_ICN", ""),
                        inspire_name=row.get("INSPIRE_name", ""),
                        decision_reason=row.get("decision_reason", ""),
                    )
                )
    return groups


# ---------------------------------------------------------------------------
# 3. Plan: for each group with a chosen leader, figure out child -> parent
# ---------------------------------------------------------------------------
@dataclass
class PlannedLink:
    ror_id: str
    ror_name: str
    parent_cn: int
    child_cn: int
    child_name: str


def build_plan(
    groups: dict[str, DupGroup],
    leaders: dict[str, int],
) -> tuple[list[PlannedLink], list[str]]:
    """
    Returns (plan, skip_reasons). skip_reasons is human-readable, one line
    per group that was skipped, so nothing silently disappears.

    Only auto_accepted members are ever considered. A group only produces
    links if it has a leader specified, has >= 2 auto_accepted members,
    and the specified leader is actually one of those auto_accepted
    members.
    """
    plan: list[PlannedLink] = []
    skips: list[str] = []

    for ror_id, group in groups.items():
        auto = group.auto_accepted

        if ror_id not in leaders:
            skips.append(f"{ror_id} ({group.ror_name}): no leader specified — skipped")
            continue

        parent_cn = leaders[ror_id]
        auto_cns = {m.control_number for m in auto}

        if len(auto) < 2:
            skips.append(
                f"{ror_id} ({group.ror_name}): only {len(auto)} auto_accepted "
                f"member(s) — nothing to link, skipped"
            )
            continue

        if parent_cn not in auto_cns:
            skips.append(
                f"{ror_id} ({group.ror_name}): chosen leader {parent_cn} is not "
                f"among this group's auto_accepted members {sorted(auto_cns)} — "
                f"skipped (check group_leaders file)"
            )
            continue

        for m in auto:
            if m.control_number == parent_cn:
                continue  # the parent doesn't get a self-referential parent link
            plan.append(
                PlannedLink(
                    ror_id=ror_id,
                    ror_name=group.ror_name,
                    parent_cn=parent_cn,
                    child_cn=m.control_number,
                    child_name=m.inspire_name or m.legacy_ICN,
                )
            )

    return plan, skips


# ---------------------------------------------------------------------------
# 4. INSPIRE GET / modify / PUT
# ---------------------------------------------------------------------------
def _make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "INSPIRE-ROR-Mapper/1.0 (CERN Summer Student; "
            "inspire-feedback@cern.ch)"
        ),
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    })
    return s


def fetch_record_with_etag(
    session: requests.Session, control_number: int
) -> tuple[dict, str]:
    """GET a single institution record. Returns (metadata, etag_header_value)."""
    url = f"{INSPIRE_API}/{control_number}"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    etag = resp.headers.get("ETag")
    if not etag:
        raise RuntimeError(
            f"No ETag header on GET {url} — cannot safely PUT without one "
            f"(would risk clobbering concurrent edits)."
        )
    data = resp.json()
    metadata = data.get("metadata", data)  # GET wraps in {"metadata": {...}}
    return metadata, etag


def strip_server_computed_fields(metadata: dict) -> dict:
    """
    Remove fields INSPIRE computes server-side that must not be PUT back.
    Operates on a copy; does not mutate the input.
    """
    out = dict(metadata)

    for key in SERVER_COMPUTED_TOP_LEVEL:
        out.pop(key, None)

    # addresses[].country is server-derived from country_code — strip if present
    if "addresses" in out:
        new_addresses = []
        for addr in out["addresses"]:
            addr = dict(addr)
            addr.pop("country", None)
            new_addresses.append(addr)
        out["addresses"] = new_addresses

    return out


def set_parent_relation(metadata: dict, parent_control_number: int) -> dict:
    """
    Return a copy of metadata with two changes applied for a child record:

    1. related_records — set exactly one relation=parent entry pointing at
       parent_control_number. Any existing non-parent relations (e.g.
       'predecessor') are preserved. Any existing parent relation is REPLACED
       (a record should have exactly one parent), with a warning if we're
       overwriting a different value.

    2. ICN — set to ["obsolete"]. The ICN field marks a record's canonical
       name in INSPIRE's authority system. Child/department records that have
       been absorbed into a parent should be marked obsolete so they no longer
       appear as independent name authorities. Confirmed from a live PUT
       capture (2026-06-30, control_number=903457): the field is a list and
       the server accepts ["obsolete"] without error.
    """
    out = dict(metadata)

    # 1. related_records: set the parent link
    existing = list(out.get("related_records", []))
    kept = [r for r in existing if r.get("relation") != "parent"]
    old_parents = [r for r in existing if r.get("relation") == "parent"]

    if old_parents:
        old_refs = [r.get("record", {}).get("$ref", "") for r in old_parents]
        log.warning(
            "  control_number=%s already had parent ref(s) %s — replacing with %s",
            metadata.get("control_number"), old_refs, parent_control_number,
        )

    new_parent_entry = {
        "relation": "parent",
        "record": {"$ref": f"{INSPIRE_API}/{parent_control_number}"},
    }
    out["related_records"] = kept + [new_parent_entry]

    # 2. ICN: mark as obsolete
    # Only log a warning if it was previously set to something meaningful,
    # so we don't spam warnings for records that already have ICN=["obsolete"]
    # or that have no ICN at all.
    old_icn = out.get("ICN", [])
    if old_icn and old_icn != ["obsolete"]:
        log.warning(
            "  control_number=%s ICN was %s — replacing with ['obsolete']",
            metadata.get("control_number"), old_icn,
        )
    out["ICN"] = ["obsolete"]

    return out


def push_parent_update(
    session: requests.Session,
    control_number: int,
    parent_control_number: int,
    live: bool,
) -> dict[str, Any]:
    """
    GET the child record, set its parent, PUT it back (if live=True).
    Returns a result dict suitable for printing / DB logging:
        {control_number, parent_control_number, status, etag, before, after, error}
    """
    result: dict[str, Any] = {
        "control_number": control_number,
        "parent_control_number": parent_control_number,
        "status": None,
        "http_status": None,
        "etag": None,
        "error": None,
    }

    try:
        metadata, etag = fetch_record_with_etag(session, control_number)
    except Exception as exc:
        result["status"] = "fetch_failed"
        result["error"] = str(exc)
        return result

    before_related = metadata.get("related_records", [])
    before_icn     = metadata.get("ICN", [])
    updated = set_parent_relation(metadata, parent_control_number)
    updated = strip_server_computed_fields(updated)

    result["etag"] = etag
    result["before_related_records"] = before_related
    result["after_related_records"]  = updated.get("related_records", [])
    result["before_icn"]             = before_icn
    result["after_icn"]              = updated.get("ICN", [])
    result["put_body_preview"] = updated

    if not live:
        result["status"] = "dry_run"
        return result

    url = f"{INSPIRE_API}/{control_number}"
    try:
        resp = session.put(
            url,
            data=json.dumps(updated),
            headers={"Content-Type": "application/json", "If-Match": etag},
            timeout=20,
        )
        result["http_status"] = resp.status_code
        if resp.status_code in (200, 201):
            result["status"] = "success"
        else:
            result["status"] = f"http_{resp.status_code}"
            result["error"] = resp.text[:500]
    except requests.RequestException as exc:
        result["status"] = "request_failed"
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# 5. DB logging (pushback_log) — self-contained, creates table if missing
# ---------------------------------------------------------------------------
def ensure_pushback_log_table(conn: sqlite3.Connection) -> None:
    """
    The project's pushback_log table already exists in inspire_ror.db
    (created elsewhere, e.g. by db_manager.py / push_to_inspire.py) with
    this schema:

        id, control_number, ror_id_written, ror_id_replaced,
        attempted_at, http_status, success, etag_used, error_message,
        dry_run

    We do NOT invent a parallel schema. If the table is missing entirely
    (e.g. a fresh DB), we create it with that exact schema so logging
    from this script is interchangeable with the rest of the project.
    If the table exists already (the normal case) we leave it untouched
    and just verify it has the columns we need to write to — if it
    doesn't, we fail loudly here rather than at INSERT time.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pushback_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            control_number   INTEGER NOT NULL,
            ror_id_written   TEXT,
            ror_id_replaced  TEXT,
            attempted_at     TEXT NOT NULL,
            http_status      INTEGER,
            success          INTEGER NOT NULL DEFAULT 0,
            etag_used        TEXT,
            error_message    TEXT,
            dry_run          INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()

    required_cols = {
        "control_number", "ror_id_written", "ror_id_replaced", "attempted_at",
        "http_status", "success", "etag_used", "error_message", "dry_run",
    }
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(pushback_log)")}
    missing = required_cols - existing_cols
    if missing:
        raise RuntimeError(
            f"pushback_log table exists but is missing column(s) {missing}. "
            f"This script writes to the project's existing schema and won't "
            f"add new columns automatically — check db_manager.py's table "
            f"definition, or update this script's INSERT to match."
        )


def log_pushback(
    conn: sqlite3.Connection,
    control_number: int,
    ror_id: str,
    result: dict[str, Any],
    live: bool,
) -> None:
    """
    Note: this is a PARENT-RELATION write, not a ROR-id write, so
    ror_id_written here records the ROR_id of the duplicate GROUP this
    parent link belongs to (for traceability back to ror_duplicates.csv),
    not a new ROR identifier being set on the child record itself.
    ror_id_replaced is left NULL — no ROR value is being replaced by this
    operation.
    """
    conn.execute(
        """
        INSERT INTO pushback_log
            (control_number, ror_id_written, ror_id_replaced, attempted_at,
             http_status, success, etag_used, error_message, dry_run)
        VALUES (?, ?, NULL, datetime('now'), ?, ?, ?, ?, ?)
        """,
        (
            control_number,
            ror_id,
            result.get("http_status"),
            1 if result.get("status") == "success" else 0,
            result.get("etag"),
            result.get("error"),
            0 if live else 1,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 6. Reporting
# ---------------------------------------------------------------------------
def print_plan(plan: list[PlannedLink], skips: list[str]) -> None:
    if skips:
        print("\n--- Skipped groups ---")
        for s in skips:
            print(f"  - {s}")

    if not plan:
        print("\nNothing to link — plan is empty.")
        return

    print(f"\n--- Planned parent links ({len(plan)} child->parent updates) ---")
    by_group: dict[str, list[PlannedLink]] = {}
    for link in plan:
        by_group.setdefault(link.ror_id, []).append(link)

    for ror_id, links in by_group.items():
        print(f"\n  {ror_id}  ({links[0].ror_name})  parent={links[0].parent_cn}")
        for link in links:
            print(f"      child {link.child_cn:>9}  \"{link.child_name}\"  -> parent {link.parent_cn}")


def print_dry_run_detail(result: dict[str, Any]) -> None:
    cn = result["control_number"]
    print(f"\n  [{cn}] dry run:")
    print(f"      before related_records: {result['before_related_records']}")
    print(f"      after  related_records: {result['after_related_records']}")
    print(f"      before ICN:             {result['before_icn']}")
    print(f"      after  ICN:             {result['after_icn']}")
    print(f"      ETag would send: {result['etag']}")


# ---------------------------------------------------------------------------
# 7. Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duplicates", default="ror_duplicates.csv", help="Path to ror_duplicates.csv")
    ap.add_argument("--leaders", required=True, help="Path to group_leaders.json or .csv")
    ap.add_argument("--db", default=DB_PATH_DEFAULT, help="Path to inspire_ror.db")
    ap.add_argument("--live", action="store_true", help="Actually perform PUT writes (default: dry run only)")
    ap.add_argument(
        "--token",
        default=os.environ.get("INSPIRE_TOKEN"),
        help="Bearer token for inspirebeta.net (or set INSPIRE_TOKEN env var)",
    )
    ap.add_argument("--delay", type=float, default=0.3, help="Seconds to sleep between PUT calls")
    args = ap.parse_args()

    groups = load_duplicate_groups(args.duplicates)
    leaders = load_group_leaders(args.leaders)
    plan, skips = build_plan(groups, leaders)
    print_plan(plan, skips)

    if not plan:
        return

    if args.live and not args.token:
        log.error(
            "Live mode requires a Bearer token. Pass --token or set INSPIRE_TOKEN env var."
        )
        sys.exit(1)

    if not args.live:
        print(
            "\nDry run only — no network writes were made. "
            "Re-run with --live (and a token) once this plan looks correct."
        )
        # Still show exact before/after for each child so you can verify
        # the PUT bodies, using a GET-only session (no token required for
        # the public read endpoint, matching fetch_inspire_records()).
        session = _make_session(args.token or "")
        for link in plan:
            result = push_parent_update(session, link.child_cn, link.parent_cn, live=False)
            print_dry_run_detail(result)
            time.sleep(0.1)
        return

    # LIVE
    confirm = input(
        f"\nAbout to PUT {len(plan)} record(s) to {INSPIRE_API} on inspirebeta.net. "
        f"Type YES to proceed: "
    )
    if confirm.strip() != "YES":
        print("Aborted.")
        return

    session = _make_session(args.token)
    conn = sqlite3.connect(args.db)
    ensure_pushback_log_table(conn)  # raises loudly up-front if schema mismatches,
                                      # rather than mid-batch after a PUT has fired

    n_ok, n_fail = 0, 0
    for link in plan:
        # PUT happens first; logging is wrapped separately so a logging
        # failure can never be confused with a write failure, and can
        # never silently swallow whether the PUT itself succeeded.
        result = push_parent_update(session, link.child_cn, link.parent_cn, live=True)

        try:
            log_pushback(conn, link.child_cn, link.ror_id, result, live=True)
        except Exception as log_exc:
            log.error(
                "  [%s] PUT result was %s, but logging to pushback_log FAILED: %s "
                "— the write itself may have still gone through; verify manually.",
                link.child_cn, result["status"], log_exc,
            )

        if result["status"] == "success":
            n_ok += 1
            print(f"  [{link.child_cn}] OK -> parent {link.parent_cn}")
        else:
            n_fail += 1
            print(f"  [{link.child_cn}] FAILED: {result['status']} {result.get('error', '')}")

        time.sleep(args.delay)

    conn.close()
    print(f"\nDone. {n_ok} succeeded, {n_fail} failed. See pushback_log table in {args.db} for details.")


if __name__ == "__main__":
    main()