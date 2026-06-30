# inspire_ror_mapper

A Python package that maps Indian institution records in
[INSPIRE HEP](https://inspirehep.net) to [ROR](https://ror.org) identifiers.

Given a list of raw INSPIRE institution metadata dicts, it retrieves ROR
candidates through a waterfall of query strategies, scores each candidate
against six independent signals, and decides whether to auto-accept or flag
for human review.

---

## Installation

```bash
pip install pandas requests rapidfuzz pgeocode
```

The package also requires `geo_scoring.py` to be present in the same directory
as the package folder (it is a project-local module, not a pip package).

---

## Package layout

```
inspire_ror_mapper/
├── __init__.py        Re-exports the full public API
├── constants.py       Thresholds, weights, reference data
├── http_utils.py      Session creation, retry logic, domain helpers
├── inspire_client.py  Fetch + parse INSPIRE records, name normalisation
├── ror_queries.py     ROR candidate retrieval — the A0–A7 query waterfall
├── scoring.py         Multi-signal scoring (score_candidate, decide)
├── pipeline.py        map_records(), flag_duplicates() — top-level orchestration
└── reporting.py       CSV exports, run summaries, single-record debug tools
```

Every public name is re-exported from `__init__.py`, so
`from inspire_ror_mapper import X` always works regardless of which
submodule `X` lives in.

---

## How matching works

Matching is a two-stage pipeline per institution:

### Stage 1 — Candidate retrieval

`get_ror_candidates()` fires a waterfall of query tiers against the ROR API,
stopping (for most tiers) once ROR's own affiliation endpoint marks a result
as `chosen`. Tiers fire in order; each one only runs if the previous tiers
haven't already produced a confident result:

| Tier | Query source | ROR endpoint | Why it exists |
|---|---|---|---|
| **A0** | Pre-existing ROR id already on the INSPIRE record | Direct org lookup | Re-verifies rather than blindly trusts existing mappings |
| **A1** | `legacy_ICN` + city + country | `?affiliation=` | Primary name source — the old INSPIRE canonical name |
| **A1b** | Modern `ICN` field + city + country | `?affiliation=` | `ICN` is often a fuller, less-abbreviated name than `legacy_ICN` |
| **A2** | Website domain | `?query.advanced=domains:X` | Exact structural field match — very high precision when ROR has it |
| **A2b** | Website domain | `?affiliation=` | Fallback when ROR's `domains` field is empty despite a real website existing in `links` |
| **A3** | Normalised `official_name` + city + country | `?affiliation=` | Independent name path from `institution_hierarchy` |
| **A4** | Normalised `official_name` | `?affiliation=` single_search | ROR's single best guess — fires when nothing is chosen yet |
| **A3b** | `name_variants` | `?affiliation=` | Fires when `official_name` is itself just an abbreviation |
| **A5** | Acronym alone + country (no city) | `?affiliation=` | Bare acronym queries — city deliberately omitted since national bodies (ISRO, BARC, DRDO) may not be registered at the satellite-centre city |
| **A6** | Acronym | `?query.advanced=names.value:"X"` | Exact structured-field match for acronym — bypasses NLP fuzzy ranking entirely |
| **A7** | `postal_address` first line | `?affiliation=` single_search | Last resort for initialism acronyms (e.g. "SBMJ" ← "Sri Bhagawan Mahaveer Jain College") where neither legacy_ICN nor the acronym appears in ROR at all |

**Why not `?query=` with a country filter?**
The previous approach used `?query=` + `country_code:IN`. It failed in two
ways: for generic names ("Indian Institute of Technology"), every IIT scored
~0.9 against every other IIT; for short acronyms ("TIFR", "BARC"), the
country filter sometimes rejected the correct record due to location metadata
inconsistencies. The affiliation endpoint uses Elasticsearch NLP, not token
overlap, and does not require an explicit country filter.

### Stage 2 — Scoring

`score_candidate()` re-scores every returned candidate using six independent
signals:

| Signal | Weight | What it checks |
|---|---|---|
| **name** | 0.25 | Fuzzy name match (`WRatio` + `token_sort_ratio` blend) across all INSPIRE and ROR name variants |
| **affiliation** | 0.25 | ROR affiliation API's own score + `chosen` flag |
| **domain** | 0.20 | Exact or partial domain overlap between INSPIRE URLs and ROR `links` |
| **ext_id** | 0.20 | Matching external identifiers (GRID, Wikidata, ISNI, ROR) |
| **location** | 0.07 | Geographic agreement via PIN code / city centroid lookup |
| **acronym** | 0.03 | Exact match between INSPIRE acronym and ROR structured acronym field |

The raw weighted sum is adjusted by a **diversity bonus**:

```
confidence = evidence_score × (0.70 + 0.30 × min(n_signals / 4, 1.0))
```

This means four independent signals at modest evidence outperform one
single strong signal — a deliberate choice to resist false positives
from, for example, a very high affiliation score alone on a generic name.

On top of the weighted sum, several **hard boosts** can raise confidence
for specific combinations of strong signals (e.g. `ext_id + domain_exact`
→ ≥ 0.92), and all boosts require `country_ok=True` to prevent a foreign
organisation from being boosted into auto-accept.

**Veto conditions** (sets `veto=True` regardless of score):

| Code | Trigger |
|---|---|
| `country_mismatch` | ROR `country_code` ≠ expected country |
| `affil_chosen_name_mismatch` | ROR says `chosen=True` but name similarity < 0.40 |
| `ext_id_country_mismatch` | Identifier match to a foreign record |
| `city_mismatch` | Both sides have a known city and they clearly differ (with escape hatches for hard identifier signals) |

### Stage 3 — Decision

`decide()` converts the best candidate's `ScoringResult` into one of:

| Outcome | Condition | `decision_reason` |
|---|---|---|
| **Auto-accept** | `confidence ≥ 0.87` AND gap to runner-up ≥ 0.04 | `auto_accepted` |
| **Auto-accept** | `ext_id:ROR` fired (deterministic re-verification) | `auto_accepted` |
| **Manual review** | Veto detected | `vetoed:<reason>` |
| **Manual review** | `0.50 ≤ confidence < 0.87` | `medium_confidence` |
| **Manual review** | `confidence ≥ 0.87` but gap too small | `high_conf_ambiguous` |
| **Discard** | `confidence < 0.50` | `low_confidence` |
| **No match** | Zero candidates returned | `no_ror_candidates_found` |

---

## Record parsing

`parse_inspire_record()` flattens a raw INSPIRE metadata dict into a clean,
typed dict. Key decisions made during parsing:

**`legacy_ICN` comma-order inversion detection**
INSPIRE's `legacy_ICN` conventionally puts the institution name first
(`"ISRO, Bangalore"`). Some legacy records invert this — either city-first
(`"Indore, Medi-Caps Inst."`, `"Bangalore, Nehru Ctr."`) or state-first
(`"Meghalaya, Natl. Inst. Tech."`). Both patterns are detected and corrected
before the string is used as a query source, using the record's own extracted
city and `INDIAN_STATES` respectively.

**State name leaking into `cities[]`**
Some records put a state name in `addresses[0].cities` instead of an actual
city (e.g. `cities=["Meghalaya"]` for NIT Meghalaya). These are detected via
`INDIAN_STATES` and cleared — a wrong city signal is worse than no city signal.

**Acronym extraction fallback**
When `institution_hierarchy` is absent, the leading all-caps token from
`legacy_ICN` is extracted as the acronym (e.g. `"SBMJ Coll., Bangalore"` →
`acronym="SBMJ"`), enabling the A5/A6 tiers to fire.

**`postal_name_candidate`**
The first line of `postal_address` is checked for structural similarity to
`legacy_ICN` via substring overlap and initialism detection. If it looks
like the institution's full name, it's stored as `postal_name_candidate`
and used as the A7 query source.

**`ICN` field normalisation**
The `ICN` field from the API is a list; it's extracted as a single string
and used as an A1b query source when it differs meaningfully from `legacy_ICN`.

---

## Name normalisation

`normalize_name()` is applied to every name before fuzzy matching:

- Expands abbreviations: `Inst.` → `Institute`, `Tech.` → `Technology`,
  `Natl.` → `National`, `Ctr.` → `Centre`, `Sci.` → `Science`, and more
- Strips `(India)` and similar country suffixes from ROR company names
- Normalises Unicode apostrophes/quotes to plain ASCII
- Strips punctuation and collapses whitespace

---

## Thresholds and weights

All configurable values live in `constants.py` and are importable directly:

```python
from inspire_ror_mapper import AUTO_ACCEPT_THRESHOLD, REVIEW_THRESHOLD, WEIGHTS
```

| Constant | Default | Meaning |
|---|---|---|
| `AUTO_ACCEPT_THRESHOLD` | `0.87` | Minimum confidence for auto-accept |
| `REVIEW_THRESHOLD` | `0.50` | Below this, candidates are not shown in the review queue |
| `GAP_THRESHOLD` | `0.04` | Minimum gap to runner-up required for auto-accept |
| `ROR_QUERY_DELAY` | `1.6` | Seconds between ROR API calls (40 req/min limit) |

---

## Public API reference

### Fetching and parsing

```python
from inspire_ror_mapper import fetch_inspire_records, parse_inspire_record

# Fetch all Indian institutions from INSPIRE (paginated automatically)
records = fetch_inspire_records(country_code="IN", max_records=None)

# Parse one raw metadata dict into a clean typed dict
parsed = parse_inspire_record(records[0])
# Keys: control_number, legacy_ICN, ICN, official_name, acronym,
#       hierarchy_names, name_variants, city, state, postal_towns,
#       postal_name_candidate, domains, raw_urls, ext_ids, existing_ror,
#       institution_type, _raw_addresses
```

### Full pipeline

```python
from inspire_ror_mapper import map_records, flag_duplicates

df = map_records(
    all_records,
    country_filter="IN",
    auto_accept_threshold=0.87,
    review_threshold=0.50,
    query_delay=1.6,
)
df = flag_duplicates(df)
```

`map_records()` returns a `pandas.DataFrame` with one row per institution:

| Column | Type | Description |
|---|---|---|
| `control_number` | int | INSPIRE control number |
| `legacy_ICN` | str | INSPIRE's legacy canonical name |
| `INSPIRE_name` | str | Normalised official name used for matching |
| `ROR_id` | str | Matched ROR id, or `""` |
| `ROR_name` | str | ROR display name, or `""` |
| `match_score` | float | Final confidence (0–1) |
| `evidence_score` | float | Raw weighted sum before diversity bonus |
| `n_signals` | int | Number of independent signals that fired |
| `match_method` | str | `+`-joined list of signals, prefixed `VETO(...)` if vetoed |
| `needs_manual_review` | bool | Whether a human needs to verify this |
| `decision_reason` | str | See decision table above |
| `ror_group_size` | int | How many INSPIRE records share this ROR id |
| `is_ror_duplicate` | bool | True if `ror_group_size > 1` |

### Scoring a single candidate

```python
from inspire_ror_mapper import score_candidate, decide, ScoringResult

result: ScoringResult = score_candidate(inspire_dict, ror_candidate_dict)
# result.confidence, result.evidence_score, result.method,
# result.n_signals, result.veto, result.veto_reason, result.country_ok

needs_review, reason = decide(result, second_best_confidence)
```

### Debugging a single record

```python
from inspire_ror_mapper import debug_candidates

# Prints every ROR candidate retrieved and its full scoring breakdown.
# Reads from inspire_records_cache.json — run_pipeline.py must have run first.
debug_candidates(907065)
```

Example output:
```
======================================================================
  INSPIRE record: 907065  —  ISRO, Bangalore
  legacy_ICN:     ISRO, Bangalore
  city:           Bangalore
  domains:        []
  ext_ids:        {'SPIRES': ['INST-53740']}
======================================================================

  11 candidate(s) returned:

  ROR ID                         ROR name                city        affil   chosen
  ---------------------------------------------------------------------------------
  https://ror.org/00cwrns71      Indian Space Research   Bengaluru   0.910   True
    conf=0.9200  evid=0.8750  n_sig=4  veto=False  method=affil_chosen+acronym+city_match+name_fuzzy
    source=A5:acronym(ISRO, India)
```

### Reporting

```python
from inspire_ror_mapper import (
    print_summary,
    export_for_manual_review,
    export_duplicate_groups,
    print_inspire_record,
)

print_summary(df)                            # terminal summary table
export_for_manual_review(df, "review.csv")  # tiered review queue CSVs
export_duplicate_groups(df, "dups.csv")     # grouped duplicate view
print_inspire_record(907065)                 # raw INSPIRE JSON for one record
```

---

## Extending the pipeline

### Adding a new query tier

All tiers live in `ror_queries.get_ror_candidates()`. To add a new one:

1. Add a block after the last tier, following the same pattern:
   ```python
   if not any(c.get("_affil_chosen") for c in candidates) and <your condition>:
       batch = _affil(<your query string>)   # or _query_exact_name() for structured lookup
       for c in batch:
           c["_query_source"] = "A8:your_tier_name(...)"
       candidates.extend(batch)
   ```
2. Document why the tier exists and exactly when it fires.
3. Run `debug_candidates()` on a record that previously had no candidates
   to verify the new tier surfaces the correct result.

### Changing a threshold

Edit the relevant constant in `constants.py` and bump `PIPELINE_VERSION`
in `run_pipeline.py`. The version string is stored in `mapping_runs` so the
DB history is queryable by version.

### Adding a new scoring signal

Add the signal computation inside `score_candidate()` in `scoring.py`,
add its weight to `WEIGHTS` in `constants.py` (and rebalance the others so
they still sum to 1.0), increment `N_SIGNAL_CATEGORIES`, and append the
signal name to `signals_fired` when it fires.

---

## Reference data

All reference data lives in `constants.py` and can be imported directly:

**`INDIAN_STATES`** — set of lowercase Indian state and union territory names.
Used to detect state-leading `legacy_ICN` inversions and to prevent state
names leaking into the city field.

**`CITY_ALIASES`** — dict mapping old/variant city names to their canonical
form (e.g. `"bangalore"` → `"bengaluru"`, `"allahabad"` → `"prayagraj"`).
Used in the city-veto logic so renamed cities are still recognised as matches.

**`GENERIC_NAMES`** — set of lowercase generic institution names (e.g.
`"national institute of technology"`, `"indian institute of technology"`).
Used to suppress boosts that would otherwise fire spuriously on generic shared
tokens when the distinctive differentiator (the specific city/campus name)
is absent or mismatched.


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
