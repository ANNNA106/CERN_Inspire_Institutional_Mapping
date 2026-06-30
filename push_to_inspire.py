"""
push_to_inspire.py
====================
Writes verified ROR mappings to the INSPIRE staging environment
(inspirebeta.net), reading from db_manager's `v_ready_for_pushback` view —
i.e. only institutions you have explicitly curated (status 'confirmed' or
'corrected') and that have NOT already been successfully pushed.

Pattern: GET record -> read ETag -> strip read-only fields -> set ROR in
external_system_identifiers -> PUT with If-Match -> log result to
pushback_log (success or failure).

IMPORTANT — payload shape (confirmed via live DevTools capture of the
actual web-UI save request, 2026-06-22):
  The PUT body is the record's metadata fields *flattened at the top
  level* — there is NO `{"metadata": {...}}` wrapper.

IMPORTANT — metadata.ror is NOT valid PUT input (confirmed via the
server's own JSONSchemaValidationError on a live 400 response,
2026-06-22): "Additional properties are not allowed ('number_of_papers',
'ror' were unexpected)". GET responses include `ror` as a computed
display field derived from external_system_identifiers, but the schema
rejects it as input. Only external_system_identifiers is written.

Also stripped before every PUT (same error, confirmed read-only):
  - metadata.number_of_papers   (computed aggregate)
  - metadata.addresses[].country (only country_code is valid input)
If a future PUT 400s on a field not in this list, push_one parses the
error message and retries once with that field stripped too, so a new
INSPIRE-side computed field doesn't require a code change to unblock.

Existing-ROR handling
----------------------
  - No existing ROR entry                 -> added.
  - Existing entry matches the new value  -> no-op.
  - Existing entry differs, --overwrite   -> REPLACED (old value logged in
                                              pushback_log.ror_id_replaced).
  - Existing entry differs, not passed    -> SKIPPED, logged as a conflict,
                                              so it surfaces for manual
                                              review instead of silently
                                              overwriting or duplicating.

Every attempt is logged, dry-run or not, so a crashed run can be resumed:
rows that already have a `success=1, dry_run=0` log entry with the same
ror_id drop out of v_ready_for_pushback automatically.

Usage
-----
    python push_to_inspire.py --token TOK                       # dry-run, no writes
    python push_to_inspire.py --token TOK --live --limit 1       # write one record, test
    python push_to_inspire.py --token TOK --live                 # write everything ready
    python push_to_inspire.py --token TOK --live --overwrite     # also replace conflicting ROR ids
"""
from __future__ import annotations

import argparse
import time

import requests

from db_manager import get_conn, log_pushback

STAGING_API = "https://inspirebeta.net/api/institutions"
ROR_SCHEMA  = "ROR"   # value used in external_system_identifiers[].schema

# Fields confirmed by the server's own JSONSchemaValidationError (2026-06-22,
# control_number=1182121) to be computed/derived and NOT valid PUT input.
# GET responses include them for display convenience, but echoing them
# back in a PUT body triggers a 400 "Additional properties are not
# allowed". Stripped before every write.
_READ_ONLY_TOP_LEVEL_FIELDS = {"ror", "number_of_papers"}
_READ_ONLY_ADDRESS_FIELDS   = {"country"}  # only country_code is valid input


def _strip_read_only_fields(metadata: dict) -> dict:
    """Remove fields the schema rejects as PUT input but that the GET
    response includes anyway (computed/denormalized display fields)."""
    cleaned = {k: v for k, v in metadata.items() if k not in _READ_ONLY_TOP_LEVEL_FIELDS}
    if "addresses" in cleaned:
        cleaned["addresses"] = [
            {k: v for k, v in addr.items() if k not in _READ_ONLY_ADDRESS_FIELDS}
            for addr in cleaned["addresses"]
        ]
    return cleaned


def _parse_unexpected_fields(error_message: str) -> set[str]:
    """Best-effort extraction of field names from an INSPIRE
    JSONSchemaValidationError 'Additional properties are not allowed
    (...)' message, so a 400 caused by a NEW unknown computed field can
    be retried automatically instead of requiring a code change every
    time INSPIRE's schema surprises us with another derived field."""
    import re
    return set(re.findall(r"'([a-zA-Z0-9_]+)' (?:were|was) unexpected", error_message))

# Be polite — this is a shared staging environment.
REQUEST_DELAY = 0.5


def _make_session(bearer_token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "INSPIRE-ROR-Mapper/1.0 (CERN Summer Student curation push)",
    })
    return s


def _get_ready_rows(limit: int | None = None,
                     control_number: int | None = None) -> list[dict]:
    conn = get_conn()
    sql = "SELECT * FROM v_ready_for_pushback"
    params: list = []
    if control_number is not None:
        sql += " WHERE control_number = ?"
        params.append(int(control_number))
    sql += " ORDER BY control_number"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def _existing_ror_value(metadata: dict) -> str | None:
    """Return the current ROR value on this record, or None if it has none.
    external_system_identifiers is the actual stored field; metadata.ror
    (when present on a GET) is a computed display field derived from it —
    not valid PUT input — so it's not used as the source of truth here."""
    for ext in metadata.get("external_system_identifiers", []):
        if (ext.get("schema") or "").upper() == ROR_SCHEMA:
            return ext.get("value")
    return None


def push_one(session: requests.Session, control_number: int, ror_id: str,
             dry_run: bool = True, allow_overwrite: bool = False) -> tuple[bool, str]:
    """
    GET the current record, set the ROR id, PUT it back with If-Match.
    Returns (success, message).

    Behaviour when an existing ROR external_system_identifier is found:
      - Same value as `ror_id`            -> no-op, logged as success.
      - Different value, allow_overwrite  -> REPLACE it (not append), log
                                              the old value in ror_id_replaced.
      - Different value, not allowed      -> SKIP, logged as a failure with
                                              a 'conflict' message, so it
                                              surfaces for manual review
                                              instead of silently creating a
                                              duplicate ROR entry.
    """
    get_url = f"{STAGING_API}/{control_number}"
    try:
        resp = session.get(get_url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_pushback(control_number, ror_id, success=False,
                      http_status=getattr(exc.response, "status_code", None),
                      error_message=f"GET failed: {exc}", dry_run=dry_run)
        return False, f"GET failed: {exc}"

    data = resp.json()
    metadata = data.get("metadata", {})
    etag = resp.headers.get("ETag")

    existing = _existing_ror_value(metadata)

    if existing == ror_id:
        log_pushback(control_number, ror_id, success=True, http_status=200,
                      etag_used=etag, error_message="already present, no-op",
                      dry_run=dry_run)
        return True, "already present — no write needed"

    if existing is not None and existing != ror_id and not allow_overwrite:
        log_pushback(control_number, ror_id, success=False, http_status=None,
                      etag_used=etag, ror_id_replaced=existing, dry_run=dry_run,
                      error_message=f"conflict: record already has ROR={existing}, "
                                     f"refusing to overwrite without --overwrite")
        return False, f"CONFLICT — record already has a different ROR ({existing}); skipped"

    # Set the ROR id in external_system_identifiers. NOTE: metadata.ror is
    # NOT valid PUT input — confirmed by the server's own schema validation
    # error (2026-06-22): it's a computed/display-only field derived from
    # external_system_identifiers at read time, not stored input.
    new_ext_ids = [
        e for e in metadata.get("external_system_identifiers", [])
        if (e.get("schema") or "").upper() != ROR_SCHEMA
    ]
    new_ext_ids.append({"schema": ROR_SCHEMA, "value": ror_id})
    metadata["external_system_identifiers"] = new_ext_ids

    replaced = existing if (existing is not None and existing != ror_id) else None

    if dry_run:
        log_pushback(control_number, ror_id, success=True, http_status=None,
                      etag_used=etag, ror_id_replaced=replaced, dry_run=True,
                      error_message="dry_run — not actually sent")
        verb = "overwrite" if replaced else "add"
        extra = f" (replacing {replaced})" if replaced else ""
        return True, f"[DRY RUN] would {verb} ROR {ror_id}{extra} in external_system_identifiers (ETag={etag})"

    # IMPORTANT: the PUT body is the metadata fields FLATTENED at the top
    # level — confirmed via live capture of the actual web-UI save request.
    # There is no {"metadata": {...}} wrapper. Computed/read-only fields
    # (ror, number_of_papers, addresses[].country, ...) are stripped — the
    # server's schema validator rejects them as "additional properties".
    put_body = _strip_read_only_fields(metadata)
    put_url = f"{STAGING_API}/{control_number}"
    headers = {"If-Match": etag} if etag else {}
    try:
        put_resp = session.put(put_url, json=put_body, headers=headers, timeout=20)
        if put_resp.status_code == 412:
            log_pushback(control_number, ror_id, success=False, http_status=412,
                         etag_used=etag, ror_id_replaced=replaced, dry_run=False,
                         error_message="412 Precondition Failed — record changed since GET")
            return False, "412 Precondition Failed (record changed concurrently — rerun to retry)"
        if put_resp.status_code == 400:
            # Try once more, stripping any additional unexpected fields the
            # error reveals — handles computed fields we haven't seen yet
            # without needing a code change for every new one INSPIRE adds.
            unexpected = _parse_unexpected_fields(put_resp.text)
            still_present = unexpected & put_body.keys()
            if still_present:
                retry_body = {k: v for k, v in put_body.items() if k not in unexpected}
                put_resp = session.put(put_url, json=retry_body, headers=headers, timeout=20)
        if not put_resp.ok:
            # Surface the actual response body — requests' default exception
            # message only gives the status line, not the JSON error detail
            # the server usually sends back (validation errors, traceback id,
            # etc). That detail is essential for diagnosing a 500 or 400.
            body_snippet = put_resp.text[:2000]
            log_pushback(control_number, ror_id, success=False, http_status=put_resp.status_code,
                         etag_used=etag, ror_id_replaced=replaced, dry_run=False,
                         error_message=f"PUT failed ({put_resp.status_code}): {body_snippet}")
            return False, f"PUT failed ({put_resp.status_code}): {body_snippet}"
    except requests.RequestException as exc:
        body_snippet = getattr(exc.response, "text", "")[:2000] if exc.response is not None else ""
        log_pushback(control_number, ror_id, success=False,
                      http_status=getattr(exc.response, "status_code", None),
                      etag_used=etag, ror_id_replaced=replaced, dry_run=False,
                      error_message=f"PUT failed: {exc} | body: {body_snippet}")
        return False, f"PUT failed: {exc} | body: {body_snippet}"

    log_pushback(control_number, ror_id, success=True, http_status=put_resp.status_code,
                 etag_used=etag, ror_id_replaced=replaced, dry_run=False)
    verb = "Overwrote" if replaced else "Added"
    extra = f" (was {replaced})" if replaced else ""
    return True, f"{verb} ROR {ror_id}{extra} — PUT succeeded ({put_resp.status_code})"


def main() -> None:
    parser = argparse.ArgumentParser(description="Push curated ROR mappings to inspirebeta.net")
    parser.add_argument("--live", action="store_true",
                         help="Actually write. Without this flag, runs in dry-run mode.")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only process the first N ready rows (good for testing).")
    parser.add_argument("--token", type=str, required=True,
                         help="Curator-level Bearer token for inspirebeta.net")
    parser.add_argument("--overwrite", action="store_true",
                         help="Allow replacing an existing ROR id that differs from the "
                              "one you're pushing. Without this flag, such rows are "
                              "skipped and logged as a conflict instead of being written.")
    parser.add_argument("--control-number", type=int, default=None,
                         help="Only push this one INSPIRE control_number, ignoring "
                              "everything else in v_ready_for_pushback. Use this for "
                              "testing on a single named institution instead of "
                              "--limit, which just takes whichever row sorts first.")
    args = parser.parse_args()

    dry_run = not args.live
    session = _make_session(args.token)

    rows = _get_ready_rows(limit=args.limit, control_number=args.control_number)
    if not rows:
        if args.control_number is not None:
            print(f"control_number {args.control_number} is not in v_ready_for_pushback — "
                  f"either it hasn't been curated (confirmed/corrected), or it's already "
                  f"been successfully pushed. Check with query_cookbook.ready_for_pushback().")
        else:
            print("Nothing to push — v_ready_for_pushback is empty.")
        return

    mode = "DRY RUN" if dry_run else "LIVE"
    ow_note = " (overwrite ENABLED)" if args.overwrite else " (overwrite disabled — conflicts will be skipped)"
    print(f"[{mode}]{ow_note} {len(rows)} institution(s) ready for pushback.\n")

    if not dry_run:
        print("About to write to inspirebeta.net for:")
        for row in rows:
            name = row.get("official_name") or row.get("legacy_ICN") or str(row["control_number"])
            print(f"    control_number={row['control_number']:<10} {name[:50]:<50} -> {row['ror_id_to_write']}")
        print()

    n_ok, n_fail, n_conflict = 0, 0, 0
    for row in rows:
        cn = row["control_number"]
        ror_id = row["ror_id_to_write"]
        name = row.get("official_name") or row.get("legacy_ICN") or str(cn)

        ok, msg = push_one(session, cn, ror_id, dry_run=dry_run, allow_overwrite=args.overwrite)
        status = "OK  " if ok else ("CONF" if "CONFLICT" in msg else "FAIL")
        print(f"  [{status}] {cn:>10}  {name[:45]:<45}  {ror_id}  — {msg}")

        n_ok += int(ok)
        n_fail += int(not ok)
        n_conflict += int("CONFLICT" in msg)
        time.sleep(REQUEST_DELAY)

    print(f"\nDone. {n_ok} succeeded, {n_fail} failed ({n_conflict} of those were conflicts).")
    if n_conflict:
        print("Conflicts were skipped, not written. Check pushback_log for ror_id_replaced "
              "values, confirm the overwrite is correct, then rerun with --overwrite.")
    if dry_run:
        print("This was a dry run — nothing was written. Re-run with --live to write for real.")


if __name__ == "__main__":
    main()