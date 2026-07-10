from __future__ import annotations

from math import radians, sin, cos, sqrt, atan2
from functools import lru_cache
import pgeocode
from .constants import COUNTRY_CODE


class GeoScorer:
    """
    Geographic matching between INSPIRE and ROR records.

    Two-stage coordinate approach:
      Stage 1 — INSPIRE campus coords (lat/lng in addresses) vs ROR city centroid
      Stage 2 — INSPIRE city name → pgeocode centroid vs ROR city centroid
                (fires only when INSPIRE has no campus coordinates)

    City string matching is intentionally removed: coordinate distance is
    a strictly better signal and avoids alias problems (Bombay/Mumbai etc).
    """

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = (
            sin(dlat / 2) ** 2
            + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        )
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))

    @staticmethod
    def _dist_to_score(dist_km: float) -> tuple[float, str]:
        """Convert a haversine distance to a (score, method) pair."""
        if dist_km <= 5:
            return 1.0, "geo_exact"
        if dist_km <= 25:
            return 0.8, "geo_near"
        if dist_km <= 100:
            return 0.5, "geo_region"
        return 0.0, "geo_far"

    @staticmethod
    @lru_cache(maxsize=512)
    def _city_centroid(city: str, country_code: str) -> tuple[float, float] | None:
        """
        Look up approximate centroid coords for a city name using pgeocode.

        pgeocode works on postal codes natively, but its underlying
        GeoNames data lets us query by place name when we pass a
        country code. Returns (lat, lng) or None if not found.

        LRU-cached so repeated lookups for the same city are free.
        """
        try:
            nomi = pgeocode.Nominatim(country_code)
            # pgeocode's query_postal_code also accepts place names
            result = nomi.query_location(city, top_k=1)
            if result is not None and not result.empty:
                lat = result.iloc[0]["latitude"]
                lng = result.iloc[0]["longitude"]
                if lat == lat and lng == lng:   # NaN check
                    return float(lat), float(lng)
        except Exception:
            pass
        return None

    def location_score(self, inspire: dict, ror: dict) -> tuple[float, str]:

        ror_lat = ror.get("lat")
        ror_lng = ror.get("lng")

        # Can't do anything without ROR city centroid coords
        if ror_lat is None or ror_lng is None:
            return 0.0, "location_none"

        # ── Stage 1: INSPIRE campus coordinates ───────────────────────────
        # These are precise geocodes from the INSPIRE addresses block.
        for addr in inspire.get("_raw_addresses", []):
            try:
                lat = float(addr["latitude"])
                lng = float(addr["longitude"])
            except (TypeError, ValueError, KeyError):
                continue

            dist = self._haversine_km(lat, lng, ror_lat, ror_lng)
            score, method = self._dist_to_score(dist)
            if score > 0:
                return score, method
            # coords exist but distance > 100km: hard geographic miss,
            # don't fall through to city lookup and give false credit
            return 0.0, "geo_far"

        # ── Stage 2: INSPIRE city name → centroid lookup ───────────────────
        # Fires only when INSPIRE has no campus coordinates.
        # Uses pgeocode to find the city centroid, then haversine vs ROR.
        # This avoids all string-matching alias problems (Bombay/Mumbai etc).
        inspire_city    = (inspire.get("city") or "").strip()
        inspire_country = (inspire.get("country_code") or COUNTRY_CODE)

        if inspire_city:
            centroid = self._city_centroid(inspire_city, inspire_country)
            if centroid is not None:
                dist = self._haversine_km(centroid[0], centroid[1], ror_lat, ror_lng)
                score, method = self._dist_to_score(dist)
                # Downgrade slightly vs campus coords since city centroid
                # vs city centroid is less precise than campus vs centroid
                if score > 0:
                    return score * 0.9, f"{method}(city_centroid)"

        return 0.0, "location_none"