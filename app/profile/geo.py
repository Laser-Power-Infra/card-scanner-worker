"""Geoapify forward-geocoding client and the strict address accept gate."""
import logging
import os
import re

import requests

log = logging.getLogger("card-desk.geo")

GEOAPIFY_URL = "https://api.geoapify.com/v1/geocode/search"

_WEAK_MATCHES = {
    "match_by_city_or_district",
    "match_by_country_or_state",
    "match_by_postcode",
}
_COARSE_TYPES = {"city", "county", "state", "country", "postcode"}


class GeoConfigError(RuntimeError):
    pass


class GeoRateLimit(Exception):
    pass


class GeoTransient(Exception):
    pass


def _api_key(api_key=None):
    key = api_key or os.environ.get("GEOAPIFY_API_KEY", "")
    if not key:
        raise GeoConfigError("GEOAPIFY_API_KEY is missing; add it to .env")
    return key


def _timeout(timeout=None):
    if timeout is not None:
        return timeout
    return int(os.environ.get("GEOAPIFY_TIMEOUT", "20"))


def geocode(address, *, api_key=None, timeout=None):
    """Forward-geocode ``address`` restricted to India.

    Returns the first result dict, or None when there are no matches.
    Raises GeoConfigError / GeoRateLimit / GeoTransient on failure."""
    key = _api_key(api_key)
    params = {
        "text": address,
        "filter": "countrycode:in",
        "lang": "en",
        "limit": 1,
        "format": "json",
        "apiKey": key,
    }
    try:
        response = requests.get(GEOAPIFY_URL, params=params,
                                timeout=_timeout(timeout))
    except requests.RequestException as exc:
        raise GeoTransient(str(exc))
    if response.status_code == 429:
        raise GeoRateLimit("HTTP 429")
    if response.status_code in (400, 401, 403):
        raise GeoConfigError("HTTP %d: %s" % (response.status_code,
                                              response.text[:200]))
    if response.status_code >= 500:
        raise GeoTransient("HTTP %d" % response.status_code)
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") or []
    return results[0] if results else None


def _normalize(text):
    return " ".join(str(text or "").lower().split())


def accept(result, company_location=None, expected=None):
    """Strict gate. Returns (ok, reason_code).

    ``expected`` is the extracted address dict; when supplied, the geocoded
    result must also agree with it (postcode, city, street/house number)."""
    if not result:
        return False, "no_results"
    if _normalize(result.get("country_code")) != "in":
        return False, "country_mismatch"
    if result.get("result_type") in _COARSE_TYPES:
        return False, "not_street_level"
    rank = result.get("rank") or {}
    if rank.get("match_type") in _WEAK_MATCHES:
        return False, "weak_match"
    try:
        if float(rank.get("confidence", 0)) < 0.5:
            return False, "low_confidence"
    except (TypeError, ValueError):
        return False, "low_confidence"
    if not result.get("street") or not result.get("postcode"):
        return False, "not_street_level"
    if expected:
        expected_post = re.sub(r"\D", "", str(expected.get("postal_code") or ""))
        result_post = re.sub(r"\D", "", str(result.get("postcode") or ""))
        if expected_post and expected_post != result_post:
            return False, "postcode_mismatch"
        exp_city = _normalize(expected.get("city"))
        res_city = _normalize(result.get("city"))
        if exp_city and exp_city not in res_city and res_city not in exp_city:
            return False, "city_mismatch"
        exp_text = _normalize(" ".join(str(expected.get(k) or "") for k in
                                        ("address_line1", "address_line2")))
        street = _normalize(result.get("street"))
        house = _normalize(result.get("housenumber"))
        tokens = [t for t in street.split() if len(t) >= 4]
        if not ((house and house in exp_text)
                or any(t in exp_text for t in tokens)):
            return False, "street_mismatch"
    hint = _normalize(company_location)
    if hint:
        state = _normalize(result.get("state"))
        state_code = _normalize(result.get("state_code"))
        names = {n for n in (state, state_code) if n}
        if not (hint in names or any(hint in n or n in hint for n in names)):
            return False, "state_mismatch"
    return True, "ok"


def extract_fields(result):
    """Flatten a Geoapify result into the enrichment columns + cache fields."""
    if not result:
        return None
    # Geoapify's address_line1 can be a POI name (hospital, mall); prefer the
    # constructed street address so a building name never masquerades as one.
    line1 = " ".join(p for p in (result.get("housenumber"),
                                 result.get("street")) if p).strip()
    if not line1:
        line1 = result.get("address_line1") or ""
    return {
        "address_line1": line1,
        "address_line2": (result.get("suburb")
                          or result.get("district") or ""),
        "city": (result.get("city") or result.get("district")
                 or result.get("county") or ""),
        "state": result.get("state") or "",
        "postal_code": result.get("postcode") or "",
        "country": result.get("country") or "",
        "place_id": result.get("place_id"),
        "latitude": result.get("lat"),
        "longitude": result.get("lon"),
        "formatted_address": result.get("formatted"),
    }
