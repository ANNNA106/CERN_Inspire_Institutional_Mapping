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
records = fetch_inspire_records(country_code=COUNTRY_CODE, max_records=None)

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
    country_filter=COUNTRY_CODE,
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