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


def check_all(result, company_location=None, expected=None):
    """Evaluate every gate check and return the full list, in gate order.

    Each item is {name, passed, detail, reason}; ``reason`` is the stable code
    accept() returns for the first failure. Unlike the old short-circuit gate,
    later checks still run so an attempt record can show every failure."""
    checks = []

    def add(name, passed, detail="", reason=None):
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": " ".join(str(detail or "").split())[:200],
            "reason": reason or name,
        })

    if not result:
        add("no_results", False, "geocoder returned no results", "no_results")
        return checks

    add("country", _normalize(result.get("country_code")) == "in",
        "country_code=%s" % (result.get("country_code") or "-"),
        "country_mismatch")

    add("street_level", result.get("result_type") not in _COARSE_TYPES,
        "result_type=%s" % (result.get("result_type") or "-"),
        "not_street_level")

    rank = result.get("rank") or {}
    add("match_type", rank.get("match_type") not in _WEAK_MATCHES,
        "match_type=%s" % (rank.get("match_type") or "-"), "weak_match")

    try:
        confidence = float(rank.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    add("confidence", confidence >= 0.5, "confidence=%s" % confidence,
        "low_confidence")

    add("street_present", bool(result.get("street")) and bool(result.get("postcode")),
        "street=%s | postcode=%s" % (result.get("street") or "-",
                                     result.get("postcode") or "-"),
        "not_street_level")

    if expected:
        expected_post = re.sub(r"\D", "", str(expected.get("postal_code") or ""))
        result_post = re.sub(r"\D", "", str(result.get("postcode") or ""))
        add("postcode", not expected_post or expected_post == result_post,
            "expected=%s | result=%s" % (expected_post or "-",
                                         result_post or "-"),
            "postcode_mismatch")

        exp_city = _normalize(expected.get("city"))
        res_city = _normalize(result.get("city"))
        add("city", not exp_city or exp_city in res_city or res_city in exp_city,
            "expected=%s | result=%s" % (exp_city or "-", res_city or "-"),
            "city_mismatch")

        exp_text = _normalize(" ".join(str(expected.get(k) or "") for k in
                                        ("address_line1", "address_line2")))
        street = _normalize(result.get("street"))
        house = _normalize(result.get("housenumber"))
        tokens = [t for t in street.split() if len(t) >= 4]
        street_ok = bool((house and house in exp_text)
                         or any(t in exp_text for t in tokens))
        add("street", street_ok,
            "expected=%s | result=%s" % (exp_text or "-", street or "-"),
            "street_mismatch")

    hint = _normalize(company_location)
    if hint:
        state = _normalize(result.get("state"))
        state_code = _normalize(result.get("state_code"))
        names = {n for n in (state, state_code) if n}
        state_ok = bool(names) and (
            hint in names or any(hint in n or n in hint for n in names))
        add("state", state_ok,
            "hint=%s | result=%s" % (hint, ",".join(sorted(names)) or "-"),
            "state_mismatch")

    return checks


def decide(checks):
    """First failed check wins, preserving the gate's original reason code."""
    for item in checks or ():
        if not item.get("passed"):
            return False, item.get("reason") or item.get("name") or "rejected"
    return True, "ok"


def accept(result, company_location=None, expected=None):
    """Strict gate. Returns (ok, reason_code).

    ``expected`` is the extracted address dict; when supplied, the geocoded
    result must also agree with it (postcode, city, street/house number)."""
    return decide(check_all(result, company_location, expected))


def result_summary(result):
    """Flatten a Geoapify result into the fields the attempt record prints."""
    if not result:
        return None
    rank = result.get("rank") or {}
    return {
        "formatted": result.get("formatted") or "",
        "lat": result.get("lat"),
        "lon": result.get("lon"),
        "resultType": result.get("result_type") or "",
        "matchType": rank.get("match_type") or "",
        "confidence": rank.get("confidence"),
        "placeId": result.get("place_id") or "",
        "postcode": result.get("postcode") or "",
        "street": result.get("street") or "",
    }


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
