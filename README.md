# INSPIRE Write-Back

Scripts for writing verified ROR mappings back to the INSPIRE staging
environment (`inspirebeta.net`). Covers two distinct write operations:

1. **ROR identifier write-back** — setting `external_system_identifiers`
   on institution records with their confirmed ROR id.
2. **Parent-child linking** — setting `related_records` (with
   `relation=parent`) and marking child `ICN` as `"obsolete"` for groups
   of INSPIRE records that share one ROR id.

Both operations follow the same safety pattern: dry run by default, live
writes require an explicit flag, every attempt is logged to `pushback_log`.

---

## Files

| File | Purpose |
|---|---|
| `db_manager.py` | SQLite schema and all DB reads/writes. Central shared dependency. |
| `bulk_confirm_auto_accepted.py` | Human gate: bulk-mark auto-accepted results as confirmed. |
| `push_to_inspire.py` | Write confirmed ROR ids to INSPIRE via GET → PUT. |
| `set_ror_group_parents.py` | Write parent-child links for ROR duplicate groups. |

---

## Database schema

```
institutions       Identity data per INSPIRE record (name, city, paper count).
                   Written by: run_pipeline.py.

mapping_runs       Full pipeline output, one row per (institution × run).
                   Append-only — reruns add rows, never overwrite.
                   Written by: run_pipeline.py.

curation           Human-verified decisions. ONE row per institution.
                   The ONLY table the write-back scripts read from.
                   Written by: YOU, via mark_curation() or bulk_confirm_auto_accepted.py.
                   The pipeline NEVER writes here.

pushback_log       Audit trail for every write-back attempt (ROR ids and
                   parent links). One row per attempt — retries, failures,
                   successes all logged.
                   Written by: push_to_inspire.py and set_ror_group_parents.py.
```

Four views derived from the tables above (never stored):

```
v_latest_run          Most recent mapping_runs row per institution.
v_auto_accepted       v_latest_run WHERE decision_reason = 'auto_accepted'.
v_manual_review       v_latest_run WHERE needs_manual_review = 1.
v_ready_for_pushback  Curated rows not yet successfully pushed.
```

`v_ready_for_pushback` is the bridge between the mapping pipeline and the
write-back scripts. A row appears here only when:
- `curation.curation_status IN ('confirmed', 'corrected')`, AND
- No `pushback_log` row exists with `success=1, dry_run=0` for this
  `control_number` and the same `ror_id_to_write`.

Successfully pushed records drop out of this view automatically — reruns
of `push_to_inspire.py` are always safe.

---

## INSPIRE PUT contract

All PUT requests to `inspirebeta.net` follow the same shape, confirmed
from live browser DevTools captures (2026-06-22, 2026-06-30):

- **URL:** `https://inspirebeta.net/api/institutions/{control_number}`
- **Body:** metadata fields **flattened at the top level** — no
  `{"metadata": {...}}` wrapper.
- **Headers required:** `Content-Type: application/json`,
  `Authorization: Bearer <token>`, `If-Match: <ETag from GET>`.
- **Fields to strip** (computed server-side, rejected by the PUT
  schema validator as `"Additional properties are not allowed"`):
  - `ror` (display-only, derived from `external_system_identifiers`)
  - `number_of_papers` (computed aggregate)
  - `addresses[].country` (derived from `country_code`)
- **`$schema` must be preserved** — INSPIRE requires it on PUT.
- **ETag / If-Match is required** for concurrency safety. A 412
  Precondition Failed response means the record was edited between
  your GET and PUT — just rerun.

---

## Part 1 — Writing ROR identifiers

### Overview

The flow for writing a ROR id is:

```
mapping pipeline (run_pipeline.py)
    → auto_accepted results
        → human review (bulk_confirm_auto_accepted.py)
            → curation table (status = 'confirmed' or 'corrected')
                → v_ready_for_pushback
                    → push_to_inspire.py
                        → inspirebeta.net PUT
                            → pushback_log
```

Nothing is pushed automatically — `bulk_confirm_auto_accepted.py` is the
explicit human decision point that gates all writes.

### Step 1 — Bulk-confirm auto-accepted results

```bash
# Preview — prints what would be confirmed, writes nothing
python3 bulk_confirm_auto_accepted.py --min-score 0.92 --limit 50

# Actually confirm (writes to the curation table)
python3 bulk_confirm_auto_accepted.py --min-score 0.92 --limit 50 --commit
```

**Flags:**

| Flag | Required | Default | Description |
|---|---|---|---|
| `--min-score` | Yes | — | Only confirm rows with `match_score ≥` this value |
| `--limit` | No | `10` | Hard cap on new confirmations in this run |
| `--commit` | No | off | Without this flag, preview only — nothing is written |
| `--curated-by` | No | `"ananya"` | Name stored in `curation.curated_by` |

The `--limit` cap is applied **after** excluding already-curated rows, so
you always get up to that many genuinely new confirmations per run.
Rerun to confirm more.

**Workflow pattern** — lower the threshold in batches rather than confirming
everything at once:

```bash
python3 bulk_confirm_auto_accepted.py --min-score 0.92 --limit 100 --commit
python3 bulk_confirm_auto_accepted.py --min-score 0.90 --limit 100 --commit
python3 bulk_confirm_auto_accepted.py --min-score 0.87 --limit 100 --commit
```

This lets you spot-check `auto_accepted.csv` at each threshold level
before committing a larger batch.

### Step 2 — Curate manual-review records

Records that weren't auto-accepted need individual decisions. Use
`mark_curation()` directly:

```python
from db_manager import mark_curation

# Pipeline's ROR suggestion is correct
mark_curation(906174, status="confirmed")

# Pipeline was wrong — supply the correct ROR id
mark_curation(906174, status="corrected",
              curated_ror_id="https://ror.org/0538gdx71")

# Checked ROR — this institution genuinely has no ROR record
mark_curation(910000, status="no_ror_exists")

# Not worth the manual lookup effort (too few papers)
mark_curation(908000, status="skip_low_priority")
```

**Status values:**

| Status | Written to INSPIRE? | When to use |
|---|---|---|
| `confirmed` | Yes | Pipeline's suggested ROR id is correct |
| `corrected` | Yes | Pipeline was wrong; provide `curated_ror_id=` |
| `no_ror_exists` | No | Checked ROR — nothing there |
| `skip_low_priority` | No | Too few papers to be worth pursuing |

Note: `status="corrected"` requires `curated_ror_id` — the function raises
`ValueError` without it.

**To undo a curation decision** (e.g. you marked something confirmed by
mistake and haven't pushed it yet):

```python
from db_manager import unmark_curation
unmark_curation(906174)
```

This refuses if the record has already been successfully pushed live —
call `unmark_curation(906174, force=True)` only if you intend to
re-curate it and push again.

**To correct a record that's already been pushed:**

```python
mark_curation(906174, status="corrected",
              curated_ror_id="https://ror.org/CORRECT_ID")
# Then rerun push_to_inspire.py --live --overwrite --control-number 906174
```

`mark_curation()` is an upsert — it overwrites the existing curation row
in place.

**Prioritisation:** to see which unmapped institutions have the most papers:

```python
from db_manager import query_unmapped_by_priority
import pandas as pd
df = query_unmapped_by_priority()
print(df.head(20))
```

### Step 3 — Check what's ready to push

```python
from db_manager import get_summary
get_summary()
```

Or query the view directly:

```python
import pandas as pd
from db_manager import get_conn
print(pd.read_sql("SELECT * FROM v_ready_for_pushback", get_conn()))
```

### Step 4 — Dry run (always do this first)

```bash
python3 push_to_inspire.py --token YOUR_TOKEN
```

Without `--live`, the script runs in dry-run mode. For each row in
`v_ready_for_pushback` it prints what it would do and logs to
`pushback_log` with `dry_run=1`. **Nothing is written to INSPIRE.**

### Step 5 — Push live

```bash
# Test with one record first
python3 push_to_inspire.py --token YOUR_TOKEN --live --control-number 906174

# Then in small batches
python3 push_to_inspire.py --token YOUR_TOKEN --live --limit 10

# Then everything remaining
python3 push_to_inspire.py --token YOUR_TOKEN --live
```

For each institution the script:
1. GETs the current INSPIRE record and reads the ETag.
2. Strips the confirmed read-only fields.
3. Adds the ROR id to `external_system_identifiers`, replacing any
   existing ROR entry (or skipping if `--overwrite` is not set and a
   different ROR id already exists).
4. PUTs the modified record with `If-Match: <ETag>`.
5. Logs the result to `pushback_log`.

**All flags:**

| Flag | Required | Description |
|---|---|---|
| `--token` | Yes | Bearer token for `inspirebeta.net` |
| `--live` | No | Actually write. Without this, dry-run only. |
| `--limit N` | No | Only process the first N rows |
| `--control-number N` | No | Push only this one institution |
| `--overwrite` | No | Allow replacing an existing different ROR id |

### Step 6 — Handle conflicts

If a record already has a **different** ROR id:

```
[CONF]  906174  Jawaharlal Nehru Centre ...  https://ror.org/0538gdx71
        — CONFLICT — record already has a different ROR (https://ror.org/XXXXX); skipped
```

Review the conflict manually. If your value is correct:

```bash
python3 push_to_inspire.py --token YOUR_TOKEN --live \
    --overwrite --control-number 906174
```

Use `--control-number` to scope `--overwrite` to just the institution
you've verified rather than applying it to everything in the queue.

**Other error types:**

| Error | Cause | Fix |
|---|---|---|
| `412 Precondition Failed` | Record was edited between GET and PUT | Just rerun — the script fetches a fresh copy |
| `400 Bad Request` | INSPIRE rejected a field | Script auto-retries once after stripping the unexpected field. If it still fails, check `pushback_log.error_message` |
| `fetch_failed` | GET request failed (network, auth) | Check your token and network access |

**Inspect all failures:**

```python
import pandas as pd
from db_manager import get_conn
print(pd.read_sql("""
    SELECT control_number, ror_id_written, attempted_at,
           http_status, error_message
    FROM pushback_log
    WHERE success = 0 AND dry_run = 0
    ORDER BY attempted_at DESC
""", get_conn()))
```

---

## Part 2 — Writing parent-child links

### Overview

When multiple INSPIRE records map to the same ROR id, they represent
sub-units of the same real-world organisation (e.g. a university and its
physics department). This script sets `related_records` with
`relation=parent` on child records and marks their `ICN` field as
`["obsolete"]` in the same PUT.

The parent-child relationship is **never inferred automatically** — you
always supply it explicitly via a `group_leaders.json` file. Only
`auto_accepted` members of a group are ever linked.

### Prerequisites

`push_to_inspire.py` must have already run (`--live`) for the group members
you want to link — parent-child links only make sense on records that
already have their ROR ids written.

### Step 1 — Understand the duplicate groups

Open `ror_duplicates.csv`. It has a `group_header` row per shared ROR id,
followed by `member` rows:

```
row_type      ROR_id                         ROR_name   group_size  control_number  INSPIRE_name
group_header  https://ror.org/04kf25f32      IIT Bombay  3
member        https://ror.org/04kf25f32      IIT Bombay              1234            Indian Inst. Tech. Bombay
member        https://ror.org/04kf25f32      IIT Bombay              5678            IIT Bombay Dept. Physics
member        https://ror.org/04kf25f32      IIT Bombay              9012            I.I.T. Bombay
```

For each group, decide: **which `control_number` is the parent** (the main
institution record, not a department)? This is a semantic decision only a
human can make — the script never guesses.

### Step 2 — Create `group_leaders.json`

```json
{
  "https://ror.org/04kf25f32": 1234,
  "https://ror.org/02np85c38": 5001,
  "https://ror.org/00cwrns71": 9900
}
```

Each key is a ROR id (from `ror_duplicates.csv`); each value is the
`control_number` of the record you want to be the parent.

A CSV format is also accepted:

```csv
ROR_id,parent_control_number
https://ror.org/04kf25f32,1234
```

You only need to list groups where you've made a decision. Groups not in
this file are skipped (printed as a skip, not an error) — build the file
incrementally.

### Step 3 — Dry run

```bash
python3 set_ror_group_parents.py \
    --duplicates ror_duplicates.csv \
    --leaders group_leaders.json \
    --token YOUR_TOKEN
```

Prints the full plan and per-record before/after diffs. Nothing is written.

Example output:

```
--- Planned parent links (2 child->parent updates) ---

  https://ror.org/04kf25f32  (IIT Bombay)  parent=1234
      child      5678  "IIT Bombay Dept. Physics"  -> parent 1234
      child      9012  "I.I.T. Bombay"             -> parent 1234

  [5678] dry run:
      before related_records: []
      after  related_records: [{'relation': 'parent', 'record': {'$ref': '.../1234'}}]
      before ICN:             ['IIT Bombay, Phys. Dept.']
      after  ICN:             ['obsolete']
      ETag would send: W/"abc123..."
```

Pay attention to `before ICN` — if the child had a meaningful ICN value,
it will be replaced with `["obsolete"]` in the same PUT.

Non-parent relations (e.g. `predecessor`) are always preserved — only the
`parent` entry is replaced.

### Step 4 — Push live

```bash
python3 set_ror_group_parents.py \
    --duplicates ror_duplicates.csv \
    --leaders group_leaders.json \
    --token YOUR_TOKEN \
    --live
```

The script prints the full plan and asks you to type `YES` before making
any network calls. There is no way to skip this confirmation.

For each child record it:
1. GETs the current INSPIRE record and reads the ETag.
2. Sets `related_records` to include one `relation=parent` entry pointing
   at the parent's `control_number`, preserving all other existing relations.
3. Sets `ICN` to `["obsolete"]`.
4. Strips server-computed fields.
5. PUTs the modified record with `If-Match: <ETag>`.
6. Logs the result to `pushback_log`.

**All flags:**

| Flag | Required | Default | Description |
|---|---|---|---|
| `--duplicates` | No | `ror_duplicates.csv` | Path to the duplicates file |
| `--leaders` | Yes | — | Path to `group_leaders.json` or `.csv` |
| `--token` | Yes (live) | `$INSPIRE_TOKEN` env var | Bearer token |
| `--live` | No | off | Actually write. Without this, dry-run only. |
| `--delay` | No | `0.3` | Seconds between PUT calls |
| `--db` | No | `inspire_ror.db` | Path to the SQLite database |

The token can also be set as an environment variable:

```bash
export INSPIRE_TOKEN=your_token_here
python3 set_ror_group_parents.py --duplicates ror_duplicates.csv --leaders group_leaders.json --live
```

### Skip reasons

The dry-run output lists every skipped group:

| Skip reason | Meaning | What to do |
|---|---|---|
| `no leader specified` | ROR id not in `group_leaders.json` | Add it when ready |
| `only 1 auto_accepted member` | Not enough qualifying members to link | Nothing to do — can't link a record to itself |
| `chosen leader not among auto_accepted members` | The `control_number` in `group_leaders.json` isn't in this group's auto-accepted members | Check `ror_duplicates.csv` and `decision_reason` for the group |

---

## Quick reference

```bash
# Check DB state at any time
python3 -c "from db_manager import get_summary; get_summary()"

# See what's in v_ready_for_pushback
python3 -c "import pandas as pd; from db_manager import get_conn; \
    print(pd.read_sql('SELECT * FROM v_ready_for_pushback', get_conn()))"

# Inspect pushback failures
python3 -c "import pandas as pd; from db_manager import get_conn; \
    print(pd.read_sql(\"SELECT * FROM pushback_log WHERE success=0 AND dry_run=0\", get_conn()))"

# Mark one record confirmed from the command line
python3 -c "from db_manager import mark_curation; mark_curation(906174, status='confirmed')"

# Correct a wrong mapping
python3 -c "from db_manager import mark_curation; \
    mark_curation(906174, status='corrected', curated_ror_id='https://ror.org/XXXXXXX')"

# Undo a curation decision (only if not yet pushed)
python3 -c "from db_manager import unmark_curation; unmark_curation(906174)"

# See full curation table
python3 -c "import pandas as pd; from db_manager import get_conn; \
    print(pd.read_sql('SELECT * FROM curation ORDER BY curated_at DESC', get_conn()))"

# See history for one institution across all pipeline runs
python3 -c "import pandas as pd; from db_manager import get_conn; \
    print(pd.read_sql('SELECT * FROM mapping_runs WHERE control_number=906174 ORDER BY run_at', get_conn()))"
```

---

## Troubleshooting

**`control_number X is not in v_ready_for_pushback`**
One of: (a) not curated yet — call `mark_curation()`; (b) already
successfully pushed — check `pushback_log`; (c) status is `no_ror_exists`
or `skip_low_priority` — not eligible.

**`sqlite3.IntegrityError: FOREIGN KEY constraint failed`**
`upsert_institutions()` didn't run before `save_mapping_run()`, or you ran
two pipeline processes against the same DB simultaneously. Fix: delete
`inspire_ror.db` and rerun `run_pipeline.py` from scratch.

**`pushback_log table exists but is missing column(s)`** (from `set_ror_group_parents.py`)
Your DB schema doesn't match what the script expects. Delete `inspire_ror.db`
and rerun `run_pipeline.py` to recreate it from the current schema.

**`FileNotFoundError: Group leaders file not found`**
Create `group_leaders.json` with at least one entry before running
`set_ror_group_parents.py`.

**`chosen leader not among auto_accepted members`**
The `control_number` you specified as parent wasn't auto-accepted in the
latest pipeline run (it may be in tier 1/2/3 review instead). Check
`ror_duplicates.csv` and verify the `decision_reason` column for that member.

**`412 Precondition Failed`**
The INSPIRE record was edited between your GET and PUT (ETag mismatch).
Simply rerun — the script will fetch a fresh copy.