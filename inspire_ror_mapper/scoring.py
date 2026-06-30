"""
Multi-signal scoring and accept/review/reject decision logic.

score_candidate() re-scores every ROR candidate returned by
ror_queries.get_ror_candidates() using five independent signals (name,
affiliation-API score, domain, external-id, location, acronym) to produce
a composite confidence that's robust to cases where the affiliation API's
own NLP ranking diverges from ground-truth signals like GRID/Wikidata IDs
and website domains.

decide() turns that score into one of: auto-accept / flag for review /
discard.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from rapidfuzz import fuzz

from .constants import (
    WEIGHTS, CITY_ALIASES, INDIAN_STATES, GENERIC_NAMES,
    AUTO_ACCEPT_THRESHOLD, REVIEW_THRESHOLD, GAP_THRESHOLD,
)
from .http_utils import domain_overlap
from .inspire_client import normalize_name

# geo_scoring.py lives alongside this package (NOT inside it) — it's an
# external module the project already has, untouched by this refactor.
# Imported lazily inside get_geo_scorer() below, exactly as the original
# single-file version did, so this package keeps resolving against
# whatever geo_scoring.py already exists in the project root.
from .geo_scoring import GeoScorer as _GeoScorer

_geo_scorer: "_GeoScorer | None" = None


def get_geo_scorer() -> "_GeoScorer":
    global _geo_scorer
    if _geo_scorer is None:
        _geo_scorer = _GeoScorer()
    return _geo_scorer


class ScoringResult(NamedTuple):
    """Full scoring output for one (INSPIRE, ROR) candidate pair."""
    confidence: float        # final value used by decide(); in [0, 1]
    evidence_score: float    # raw weighted sum before diversity + boost
    method: str              # "+"-joined list of signals that fired
    n_signals: int           # number of distinct signal categories that fired
    veto: bool               # True if a hard contradiction was detected
    veto_reason: str         # human-readable veto explanation, or ""
    country_ok: bool         # ROR candidate country matches expected


def score_candidate(
    inspire: dict,
    ror: dict,
    expected_country: str = "IN",
) -> ScoringResult:
    """
    Score one (INSPIRE record, ROR candidate) pair.

    Returns a ScoringResult.  The field ``confidence`` is the value that
    decide() uses; ``evidence_score`` is the raw weighted sum.

    Key design changes vs the previous composite-only approach
    ----------------------------------------------------------
    1. Country veto: any candidate whose ROR country_code != expected_country
       is flagged veto=True.  Hard boosts never fire for foreign candidates.
       This prevents the BARC → French nuclear department false positive.

    2. Signal diversity bonus: confidence = evidence × (0.7 + 0.3 × diversity)
       where diversity = min(n_signals / 4, 1.0).  Four independent signals
       at modest evidence outperform one strong signal at high evidence.

    3. Hard boosts require country_ok=True.  Their name-score floors are
       tightened (ext_id: 0.60→unchanged, domain: 0.55→0.60, affil: 0.75→0.75)
       to reduce false-positive boost firing.

    4. New multi-signal boost: ext_id + domain + affil≥0.70 + country_ok →
       confidence ≥ 0.92.  This is the fix for IIT Hyderabad-style cases
       where three hard signals agree but the name is abbreviated.

    5. Affil-chosen + weak name veto: if ROR says chosen=True but name_score
       < 0.40, we set veto=True with reason "affil_chosen_name_mismatch".
       The API can be confidently wrong; a near-zero name match is a red flag.
    """
    signals_fired: list[str] = []

    # ── Country check (runs first — needed by all boost conditions) ───────
    ror_country  = (ror.get("country_code") or "").upper().strip()
    country_ok   = (ror_country == expected_country.upper()) if expected_country else True

    # ── Name score ────────────────────────────────────────────────────────
    # WRatio: weighted blend sensitive to both full-string and partial matches
    # without over-rewarding shared generic tokens.
    inspire_names = [
        n for n in (
            [inspire["official_name"]]
            + inspire["hierarchy_names"]
            + inspire["name_variants"]
            + [inspire.get("postal_name_candidate", "")]
        ) if n
    ]

    best_name = 0.0
    inspire_city = (inspire.get("city") or "").strip()

    for iname in inspire_names:
        iname_norm = normalize_name(iname)

        # Build variants: original normalized + city-suffix-stripped
        # INSPIRE names embed city as suffix ("St Xavier's College Ahmedabad"
        # from "St. Xavier's Coll., Ahmedabad"), penalising token_sort_ratio
        # against bare ROR names. Stripped form catches these cases.
        iname_variants = [iname_norm]
        if inspire_city and iname_norm.lower().endswith(inspire_city.lower()):
            stripped = iname_norm[: -len(inspire_city)].strip()
            if stripped and normalize_name(stripped).lower() not in GENERIC_NAMES:
                iname_variants.append(stripped)

        for rname in ror["all_names"]:
            rname_norm = normalize_name(rname)

            # Build ROR name variants: original + city-stripped form.
            # "Gurugram University" → strip "Gurugram" → "University"
            # This prevents "SGT University Gurugram" (INSPIRE) from falsely
            # matching "Gurugram University" (ROR) at name_exact level:
            # both share tokens {"University", "Gurugram"} making WRatio=95,
            # but the distinctive token "SGT" is ignored by token reordering.
            # When we strip the city from INSPIRE ("SGT University") and from
            # ROR ("University"), the comparison correctly scores ~75 (fuzzy).
            rname_variants = [rname_norm]
            if inspire_city and rname_norm.lower().startswith(inspire_city.lower()):
                rname_stripped = rname_norm[len(inspire_city):].strip()
                if rname_stripped and normalize_name(rname_stripped).lower() not in GENERIC_NAMES:
                    rname_variants.append(rname_stripped)

            for iv in iname_variants:
                for rvv in rname_variants:
                    w  = fuzz.WRatio(iv, rvv) / 100.0
                    ts = fuzz.token_sort_ratio(iv, rvv) / 100.0
                    if w - ts > 15:
                        s = (w * 0.35 + ts * 0.65)
                    else:
                        s = (w + ts) / 2.0
                    if s > best_name:
                        best_name = s

    # ── Substring + city match upgrade ───────────────────────────────────
    # If city_match fires (computed later) and one name is a pure substring
    # of the other, the difference is just an embedded city name — treat as
    # name_exact. We pre-compute this flag here and apply it after city scoring.
    # Examples:
    #   "Anna University" ⊂ "Anna University Chennai"  + city=Chennai → exact
    #   "St Xavier's College" ⊂ "St Xavier's College Ahmedabad" + city=Ahmedabad → exact
    _substring_name_match = False
    if best_name < 0.90:  # only matters when currently below name_exact threshold
        inspire_city_lower = (inspire.get("city") or "").strip().lower()
        if inspire_city_lower:
            for iname in inspire_names:
                in_norm = normalize_name(iname).lower()
                for rname_raw in ror["all_names"]:
                    rn_norm = normalize_name(rname_raw).lower()
                    # Check if one is substring of other, and the extra tokens
                    # are just the city name
                    if in_norm in rn_norm or rn_norm in in_norm:
                        longer  = rn_norm if len(rn_norm) > len(in_norm) else in_norm
                        shorter = in_norm if len(rn_norm) > len(in_norm) else rn_norm
                        extra   = longer.replace(shorter, "").strip().strip(",").strip()
                        if extra == inspire_city_lower:
                            _substring_name_match = True
                            break
                if _substring_name_match:
                    break

    name_score = best_name
    if best_name >= 0.90:
        signals_fired.append("name_exact")
    elif best_name >= 0.75:
        signals_fired.append("name_fuzzy")
    elif best_name >= 0.55:
        signals_fired.append("name_weak")  # track even weak name evidence

    # ── Affiliation API score ─────────────────────────────────────────────
    affil_raw    = float(ror.get("_affil_score", 0.0))
    affil_chosen = bool(ror.get("_affil_chosen", False))
    # Floor affil_score at 0.80 when chosen=True, but only when the name is
    # not contradicting it (name_score < 0.40 will trigger a veto below).
    affil_score = affil_raw
    if affil_chosen:
        affil_score = max(affil_score, 0.80)
        signals_fired.append("affil_chosen")
    elif affil_raw >= 0.50:
        signals_fired.append(f"affil({affil_raw:.2f})")
    elif affil_raw >= 0.30:
        signals_fired.append(f"affil_low({affil_raw:.2f})")  # still record it

    # ── Domain / website score ────────────────────────────────────────────
    domain_score = 0.0
    inspire_has_domain = bool(inspire["domains"])
    ror_has_domain     = bool(ror["domains"])

    if inspire_has_domain and ror_has_domain:
        # Both sides have domain data — a real comparison is possible
        for idomain in inspire["domains"]:
            for rdomain in ror["domains"]:
                if idomain == rdomain:
                    domain_score = 1.0
                    break
                if domain_overlap(idomain, rdomain):
                    domain_score = max(domain_score, 0.5)
            if domain_score == 1.0:
                break
        # If both have domains and they don't overlap — that's a soft negative signal
        # (not a hard veto, but the absence of a match is meaningful)
        domain_absent = False
    else:
        # One or both sides have no domain data — silence, not contradiction
        domain_absent = True

    if domain_score >= 1.0:
        signals_fired.append("domain_exact")
    elif domain_score > 0:
        signals_fired.append("domain_partial")

    # ── External identifier score ─────────────────────────────────────────
    ext_score      = 0.0
    matched_schema = ""

    def _bare_ror_id(v: str) -> str:
        """Strip https://ror.org/ prefix to get the bare 9-char id."""
        return v.rstrip("/").split("/")[-1].lower()

    # Special case: INSPIRE carries a ROR URL in external_system_identifiers.
    # ROR records never self-report their own ID in external_ids, so we
    # compare directly against ror["ror_id"] (the candidate's own ROR ID).
    inspire_ror_ids = inspire["ext_ids"].get("ROR", [])
    if inspire_ror_ids:
        candidate_bare = _bare_ror_id(ror.get("ror_id", ""))
        if any(_bare_ror_id(v) == candidate_bare for v in inspire_ror_ids):
            ext_score      = 1.0
            matched_schema = "ROR"

    # All other schemas (GRID, Wikidata, ISNI, etc.): cross-check against
    # the ROR record's external_ids as before.
    if not ext_score:
        for schema, ivalues in inspire["ext_ids"].items():
            if schema == "ROR":
                continue  # already handled above
            rvalues = ror["ext_ids"].get(schema, [])
            if set(ivalues) & set(rvalues):
                ext_score      = 1.0
                matched_schema = schema
                break

    if ext_score:
        signals_fired.append(f"ext_id:{matched_schema}")

    ext_absent = not any(inspire["ext_ids"].values())

    # ── Location score ────────────────────────────────────────────────────
    loc_score, loc_method = get_geo_scorer().location_score(inspire, ror)
    if loc_score > 0:
        signals_fired.append(loc_method)

    # ── Acronym score ─────────────────────────────────────────────────────
    acr_score = 0.0
    if inspire["acronym"] and ror["acronyms"]:
        if any(inspire["acronym"].upper() == a.upper() for a in ror["acronyms"]):
            acr_score = 1.0
    if acr_score:
        signals_fired.append("acronym")

    # ── City mismatch veto (new) ──────────────────────────────────────────
    # If both INSPIRE and ROR have a city and they differ significantly,
    # this is strong evidence of a wrong match. Don't auto-accept across cities.
    # Uses rapidfuzz for normalised comparison to handle "Barasat" vs "Barasat"
    # spelling variants while catching "Barasat" vs "Salem" clearly.
    inspire_city = (inspire.get("city") or "").strip()
    ror_city     = (ror.get("city")     or "").strip()

    def _canonical_city(city: str) -> str:
        """Strip state suffix and apply known rename/alias."""
        c = city.lower().split(",")[0].strip()
        return CITY_ALIASES.get(c, c)

    inspire_city_canon = _canonical_city(inspire_city)
    ror_city_canon     = _canonical_city(ror_city)

    city_match_score = 0.0
    if inspire_city and ror_city:
        # Score on canonical (alias-resolved) names so renamed cities like
        # Bangalore/Bengaluru, Bombay/Mumbai, Calcutta/Kolkata, Allahabad/
        # Prayagraj are recognised as matches, not just exempted from veto.
        city_match_score = fuzz.token_sort_ratio(inspire_city_canon, ror_city_canon) / 100.0
        if city_match_score >= 0.85:
            signals_fired.append("city_match")

    # ── Postal address city fallback ──────────────────────────────────────
    # INSPIRE sometimes stores district in cities[] but actual town in
    # postal_address (e.g. cities=["Hooghly"] but postal has "Serampore").
    # Conversely, INSPIRE may store the specific town in cities[] while ROR
    # geocodes to the broader district city (e.g. INSPIRE city="Banki" but
    # ROR city="Cuttack", with postal_address="Banki, Cuttack, INDIA").
    # Whole-line similarity dilutes short city names inside longer comma-
    # separated address lines, so we check token-level containment instead.
    def _city_in_text(city: str, text: str) -> bool:
        tokens = re.findall(r"[A-Za-z]+", text)
        return any(fuzz.ratio(city.lower(), tok.lower()) >= 90 for tok in tokens)

    _postal_towns = inspire.get("postal_towns", [])
    _ror_city_in_postal = bool(ror_city) and any(
        fuzz.token_sort_ratio(ror_city.lower(), t.lower()) >= 85
        or _city_in_text(ror_city, t)
        for t in _postal_towns
    )
    _city_confirmed = city_match_score >= 0.85 or _ror_city_in_postal

    # Apply substring+city upgrade: if city matched and names differ only by
    # the city suffix/prefix, upgrade to name_exact
    if _substring_name_match and _city_confirmed:
        best_name  = 1.0
        name_score = 1.0
        # Replace name signal with name_exact
        if "name_fuzzy" in signals_fired:
            signals_fired.remove("name_fuzzy")
        if "name_weak" in signals_fired:
            signals_fired.remove("name_weak")
        if "name_exact" not in signals_fired:
            signals_fired.append("name_exact")

    # ── Raw evidence score (weighted sum) ────────────────────────────────
    evidence_score = (
        WEIGHTS["name"]        * name_score
        + WEIGHTS["affiliation"] * affil_score
        + WEIGHTS["domain"]      * domain_score
        + WEIGHTS["ext_id"]      * ext_score
        + WEIGHTS["location"]    * loc_score
        + WEIGHTS["acronym"]     * acr_score
    )

    # ── Signal diversity bonus ────────────────────────────────────────────
    # Count how many *distinct* signal categories contributed meaningfully
    # (not just "fired" — we want signals that actually moved the score).
    n_signals = sum([
        name_score  >= 0.55,   # even weak name counts if it clears 0.55
        affil_score >= 0.50,
        domain_score > 0,
        ext_score == 1.0,
        loc_score   > 0,
        acr_score == 1.0,
    ])

    # diversity in [0, 1]; saturates at 4 independent signals
    # Formula: confidence = evidence × (0.70 + 0.30 × min(n_signals/4, 1))
    # At n=1: multiplier=0.775  →  a single strong signal is discounted
    # At n=4: multiplier=1.0    →  four signals get full face value
    diversity   = min(n_signals / 4.0, 1.0)
    confidence  = evidence_score * (0.70 + 0.30 * diversity)

    # ── Hard boosts (country-gated) ───────────────────────────────────────
    # All boosts require country_ok=True.  A French institution can never
    # be boosted into auto-accept for an Indian INSPIRE record.
    if country_ok:
        # Three hard signals agree (strongest possible case):
        # ext_id identifies the org, domain corroborates it, affil API agrees.
        if ext_score == 1.0 and domain_score == 1.0 and affil_score >= 0.70:
            confidence = max(confidence, 0.92)

        # ext_id + plausible name: identifier match is nearly deterministic;
        # name floor 0.60 guards against stale/reassigned identifiers.
        if ext_score == 1.0 and name_score >= 0.60:
            confidence = max(confidence, 0.90)

        # domain_exact + ext_id:ROR: two independent identifier-class signals
        # agree, with no affil_score dependency (handles A0-sourced candidates
        # where affil was never queried).
        if domain_score == 1.0 and ext_score == 1.0:
            confidence = max(confidence, 0.92)

        # affil_chosen + strong name + geo agreement.
        # City guard prevents NIT Tiruchirappalli alias matches from boosting
        # REC Rourkela/Durgapur/Jalandhar via "Regional Engineering College
        # Tiruchirappalli" historical alias in ROR.
        if (
            affil_chosen
            and name_score >= 0.75
            and (
                city_match_score >= 0.50  # cities agree, or
                or not inspire_city       # INSPIRE has no city data
                or not ror_city           # ROR has no city data
            )
        ):
            confidence = max(confidence, 0.85)

        # domain-only boost: domain match is near-deterministic for Indian institutions
        # (vit.ac.in can only belong to VIT). No affil required since domain lookup
        # bypasses the affiliation API entirely.
        if domain_score == 1.0 and name_score >= 0.40:
            confidence = max(confidence, 0.88)

        # name_exact + strong affil: base floor for data-sparse institutions.
        if name_score >= 0.90 and affil_score >= 0.75:
            confidence = max(confidence, 0.52)

        # name_exact + strong affil + city_match: three independent signals agree.
        # City match is a meaningful corroborating signal even when domain/ext_id
        # are absent or mismatched. This catches cases like Anna University where
        # affil=0.96, name_exact, city_match all agree but affil_chosen didn't fire
        # and domain doesn't match exactly.
        if name_score >= 0.90 and affil_score >= 0.75 and _city_confirmed:
            confidence = max(confidence, 0.88)

        if (
            domain_absent
            and ext_absent
            and name_score >= 0.90
            and affil_score >= 0.75
            and loc_score > 0
        ):
            confidence = max(confidence, 0.88)
            # also flag in method so reviewers can see why it was boosted
            signals_fired.append("data_sparse_boost")

        if (
            domain_absent
            and ext_absent
            and name_score >= 0.75
            and affil_score >= 0.75
            and loc_score >= 1.0   # geo_exact only
        ):
            confidence = max(confidence, 0.92)  # straight to auto-accept
            signals_fired.append("data_sparse_boost")

        if (
            "name_exact" in signals_fired
            and "acronym"  in signals_fired
            and affil_score >= 0.80
            and inspire["official_name"].lower() not in GENERIC_NAMES
            and len(inspire.get("acronym", "")) >= 4  # avoid boosting short/ambiguous acronyms like "IIT"
        ):
            confidence = max(confidence, 0.92)

        if domain_score >= 0.5 and name_score >= 0.90 and city_match_score >= 0.50:
            confidence = max(confidence, 0.88)

    # ── Veto detection ────────────────────────────────────────────────────
    # Veto = a hard contradiction that should block auto-accept regardless of
    # the evidence score.  Vetoed records always go to manual review.
    veto        = False
    veto_reason = ""

    # V1: Country mismatch — the single most important veto.
    # Applied when we have an explicit expected country and the ROR record
    # has a country_code that differs.  Records where ROR has no country_code
    # at all are not vetoed (unknown ≠ wrong) but will not receive boosts.
    if expected_country and ror_country and not country_ok:
        veto        = True
        veto_reason = f"country_mismatch:{ror_country}≠{expected_country}"

    # V2: Affiliation API is "chosen" but names don't even weakly match.
    # The API can be confidently wrong (e.g., nuclear physics keyword overlap).
    # A name score below 0.40 means the strings share almost nothing — this
    # is the BARC→French department signature.
    if affil_chosen and name_score < 0.40 and not veto:
        veto        = True
        veto_reason = f"affil_chosen_name_mismatch:name={name_score:.2f}"

    # V3: ext_id fired but country doesn't match.
    # An identifier match to a foreign institution means the INSPIRE record
    # may have a wrong ext_id, or the ROR record covers a foreign branch.
    # Flag for review; do not block entirely (could be a valid foreign branch).
    if ext_score == 1.0 and not country_ok and not veto:
        veto        = True
        veto_reason = f"ext_id_country_mismatch:{ror_country}≠{expected_country}"

    inspire_city_canon = _canonical_city(inspire_city)
    ror_city_canon     = _canonical_city(ror_city)

    # Hard identifier signals that already confirm institution identity —
    # if any of these fired, a city mismatch is not sufficient grounds to veto
    # (e.g. campus location differs from headquarters city, or INSPIRE's city
    # field is stale/imprecise, but the ROR ID or domain match is conclusive).
    _hard_signals    = {"ext_id:ROR", "ext_id:GRID", "domain_exact"}
    _has_hard_signal = bool(_hard_signals & set(signals_fired))

    # V4: City mismatch — both sides have a known city and they clearly differ.
    # Only fires for same-country candidates (cross-country caught by V1).
    # Prevents false matches where name is generic and only city disambiguates
    # e.g. "Mahatma Gandhi University" exists in Kottayam, Nalgonda, Meghalaya.
    # Also prevents affil_chosen alias matches e.g. NIT Tiruchirappalli matched
    # to REC Rourkela via "Regional Engineering College Tiruchirappalli" alias.
    if (
        not veto
        and country_ok
        and inspire_city_canon and ror_city_canon
        and city_match_score < 0.50
        and inspire_city_canon.lower() not in INDIAN_STATES
        and ror_city_canon.lower() not in INDIAN_STATES
        and not _has_hard_signal
        and loc_score == 0.0
        and not _ror_city_in_postal                     # ROR city found in postal address
    ):
        veto        = True
        veto_reason = f"city_mismatch:{inspire_city}≠{ror_city}"

    method = "+".join(signals_fired) if signals_fired else "no_signal"
    if veto:
        method = f"VETO({veto_reason})|" + method

    return ScoringResult(
        confidence    = round(confidence, 4),
        evidence_score= round(evidence_score, 4),
        method        = method,
        n_signals     = n_signals,
        veto          = veto,
        veto_reason   = veto_reason,
        country_ok    = country_ok,
    )


def decide(
    best:          ScoringResult,
    second_conf:   float,
    auto_accept:   float = AUTO_ACCEPT_THRESHOLD,
    review_floor:  float = REVIEW_THRESHOLD,
    gap_threshold: float = GAP_THRESHOLD,
) -> tuple[bool, str]:
    """
    Decide whether to auto-accept, flag for review, or reject a candidate.

    Parameters
    ----------
    best          : ScoringResult for the top-ranked candidate.
    second_conf   : confidence of the runner-up (0.0 if there is none).
    auto_accept   : confidence threshold for auto-accept.
    review_floor  : confidence threshold below which we do not show the
                    candidate to a human reviewer (too weak to be useful).
    gap_threshold : minimum gap between best and second confidence required
                    for auto-accept (prevents accepting when two candidates
                    are nearly tied).

    Returns
    -------
    (needs_manual_review, reason_string)
    """
    conf = best.confidence
    gap  = conf - second_conf

    # ── Veto overrides everything ─────────────────────────────────────────
    # A vetoed record always goes to manual review.  It is never auto-accepted
    # or silently discarded.
    if best.veto:
        return True, f"vetoed:{best.veto_reason}"

    # ── Auto-accept ───────────────────────────────────────────────────────
    if conf >= auto_accept:
        # ext_id:ROR is deterministic — INSPIRE already carries this ROR ID,
        # so even a small gap to the runner-up is irrelevant. The correct
        # record is already known; we are just re-verifying it.
        if "ext_id:ROR" in best.method:
            return False, "auto_accepted"

        if gap >= gap_threshold:
            return False, "auto_accepted"

        # Cleared threshold but gap to runner-up is too small.
        return True, f"high_conf_ambiguous:gap={gap:.3f}"

    # ── Medium confidence ─────────────────────────────────────────────────
    if conf >= review_floor:
        return True, "medium_confidence"

    # ── Low confidence ────────────────────────────────────────────────────
    # Below the review floor — not useful enough to show to a human reviewer.
    # The record will still appear in the full CSV with an empty ROR_id.
    return True, "low_confidence"
