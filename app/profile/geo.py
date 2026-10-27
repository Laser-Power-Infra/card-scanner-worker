"""Geoapify forward-geocoding client and the strict address accept gate."""
import logging
import math
import os
import re

import requests

from . import pin_codes

log = logging.getLogger("card-desk.geo")

GEOAPIFY_URL = "https://api.geoapify.com/v1/geocode/search"

VERIFIED_KM = float(os.environ.get("ADDRESS_VERIFIED_KM", "0.5"))
APPROXIMATE_KM = float(os.environ.get("ADDRESS_APPROXIMATE_KM", "25"))
# A street-level match can survive a coarse/wrong PIN centroid further than an
# area/city result, but not indefinitely.
STREET_AREA_KM = float(os.environ.get("ADDRESS_STREET_AREA_KM", "50"))

_WEAK_MATCHES = {
    "match_by_city_or_district",
    "match_by_country_or_state",
    "match_by_postcode",
}
_COARSE_TYPES = {"city", "county", "state", "country", "postcode"}
_STREET_TYPES = {"building", "amenity", "house", "street", "road", "highway"}
_AREA_TYPES = {"suburb", "district", "locality", "neighbourhood", "quarter",
               "borough", "postcode"}


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


def geocode_many(address, *, api_key=None, timeout=None, limit=5,
                 country_filter="in"):
    """Forward-geocode ``address`` and return up to ``limit`` result dicts.

    Raises GeoConfigError / GeoRateLimit / GeoTransient on failure."""
    key = _api_key(api_key)
    params = {
        "text": address,
        "lang": "en",
        "limit": max(1, min(int(limit), 10)),
        "format": "json",
        "apiKey": key,
    }
    if country_filter:
        params["filter"] = "countrycode:" + str(country_filter)
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
    return response.json().get("results") or []


def geocode(address, *, api_key=None, timeout=None):
    """Forward-geocode ``address`` restricted to India.

    Returns the first result dict, or None when there are no matches.
    Raises GeoConfigError / GeoRateLimit / GeoTransient on failure."""
    results = geocode_many(address, api_key=api_key, timeout=timeout, limit=1)
    return results[0] if results else None


_INDIA_HINTS = (
    "west bengal", "telangana", "maharashtra", "karnataka", "tamil nadu",
    "delhi", "gujarat", "uttar pradesh", "andhra pradesh", "rajasthan",
    "madhya pradesh", "haryana", "punjab", "kerala", "odisha", "bihar",
    "jharkhand", "assam", "chhattisgarh", "goa", "uttarakhand",
    "himachal pradesh", "jammu and kashmir", "chandigarh", "india",
    "hyderabad", "secunderabad", "kolkata", "mumbai", "pune", "chennai",
    "bengaluru", "bangalore", "ahmedabad", "jaipur", "lucknow",
    "gurugram", "noida", "kochi", "coimbatore", "indore", "bhopal",
    "patna", "ranchi", "guwahati", "visakhapatnam", "vijayawada",
    "nagpur", "surat", "vadodara", "ludhiana", "amritsar", "dehradun",
    "shimla", "srinagar", "bhubaneswar", "raipur", "panaji",
    "thiruvananthapuram", "kolhapur", "manipur", "meghalaya",
)


_COUNTRY_CODES = {
    "united arab emirates": "ae", "dubai": "ae", "abu dhabi": "ae", "uae": "ae",
    "kuwait": "kw", "oman": "om", "sultanate of oman": "om",
    "qatar": "qa", "saudi arabia": "sa", "saudi": "sa", "riyadh": "sa",
    "jeddah": "sa", "bahrain": "bh",
    "egypt": "eg", "giza": "eg", "cairo": "eg",
    "united states": "us", "usa": "us", "colorado": "us", "idaho": "us",
    "california": "us", "texas": "us", "new york": "us",
    "canada": "ca", "united kingdom": "gb", "uk": "gb", "london": "gb",
    "singapore": "sg", "malaysia": "my", "australia": "au",
    "germany": "de", "france": "fr", "italy": "it", "spain": "es",
    "netherlands": "nl", "china": "cn", "japan": "jp", "hong kong": "hk",
    "south africa": "za", "kenya": "ke", "nigeria": "ng",
    "bangladesh": "bd", "sri lanka": "lk", "nepal": "np", "pakistan": "pk",
}

_COUNTRY_NAMES = {
    "in": "India", "ae": "United Arab Emirates", "kw": "Kuwait",
    "om": "Oman", "qa": "Qatar", "sa": "Saudi Arabia", "bh": "Bahrain",
    "eg": "Egypt", "us": "United States", "ca": "Canada",
    "gb": "United Kingdom", "sg": "Singapore", "my": "Malaysia",
    "au": "Australia", "de": "Germany", "fr": "France", "it": "Italy",
    "es": "Spain", "nl": "Netherlands", "cn": "China", "jp": "Japan",
    "hk": "Hong Kong", "za": "South Africa", "ke": "Kenya",
    "ng": "Nigeria", "bd": "Bangladesh", "lk": "Sri Lanka", "np": "Nepal",
    "pk": "Pakistan",
}


def country_filter_for(company_location, company=""):
    """Return the expected ISO country code; default India."""
    text = _normalize(company_location)
    name = _normalize(company)
    if not text:
        return "in"
    if any(hint in text or hint in name for hint in _INDIA_HINTS):
        return "in"
    for hint, code in _COUNTRY_CODES.items():
        if hint in text:
            return code
    return "in"


def country_name_for(code):
    return _COUNTRY_NAMES.get(_normalize(code), "")


def _normalize(text):
    return " ".join(str(text or "").lower().split())


# Suburbs that are administratively separate but part of the same metro for
# address purposes. A "New Town" result is not a city contradiction with a
# "Kolkata" contact.
_CITY_METRO = {
    "new town": "kolkata", "newtown": "kolkata", "rajarhat": "kolkata",
    "salt lake": "kolkata", "bidhannagar": "kolkata",
    "madhapur": "hyderabad", "gachibowli": "hyderabad",
    "kondapur": "hyderabad", "hitech city": "hyderabad",
    "secunderabad": "hyderabad", "kukatpally": "hyderabad",
    "whitefield": "bengaluru", "electronic city": "bengaluru",
    "hinjewadi": "pune", "pimpri": "pune", "pimpri chinchwad": "pune",
}


def _city_key(value):
    key = _normalize(value)
    return _CITY_METRO.get(key, key)


def same_city(first, second):
    """Compare city names, allowing known metro/suburb aliases."""
    a, b = _city_key(first), _city_key(second)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def result_precision(result):
    """STREET / AREA / CITY from what the geocoder actually returned.

    This is deliberately independent of the extracted address line: an
    extracted street line is an input, not proof that the saved pin is
    street-level.
    """
    if not result:
        return "CITY"
    result_type = _normalize(result.get("result_type"))
    match_type = _normalize((result.get("rank") or {}).get("match_type"))
    if (result.get("housenumber")
            or result_type in _STREET_TYPES
            or (match_type in {"full_match", "match_by_building",
                               "match_by_street"} and result.get("street"))):
        return "STREET"
    if (result_type in _AREA_TYPES or result.get("suburb")
            or result.get("district")
            or match_type in {"match_by_postcode"}):
        return "AREA"
    return "CITY"


def check_all(result, company_location=None, expected=None, country_code="in"):
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

    expected_country = _normalize(country_code)
    add("country",
        (not expected_country
         or _normalize(result.get("country_code")) == expected_country),
        "expected=%s | result=%s" % (expected_country or "-",
                                     result.get("country_code") or "-"),
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

        add("postcode_valid",
            (not result_post or expected_country != "in"
             or pin_codes.is_valid(result_post)),
            "result=%s" % (result_post or "-"), "invalid_postcode")

        exp_city = _normalize(expected.get("city"))
        res_city = _normalize(result.get("city"))
        add("city", not exp_city or same_city(exp_city, res_city),
            "expected=%s | result=%s" % (exp_city or "-", res_city or "-"),
            "city_mismatch")

        exp_state = _normalize(expected.get("state"))
        state = _normalize(result.get("state"))
        state_code = _normalize(result.get("state_code"))
        names = {n for n in (state, state_code) if n}
        expected_state_ok = (not exp_state or not names
                             or exp_state in names
                             or any(exp_state in n or n in exp_state
                                    for n in names))
        add("expected_state", expected_state_ok,
            "expected=%s | result=%s" % (
                exp_state or "-", ",".join(sorted(names)) or "-"),
            "state_mismatch")

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
    if hint and expected_country == "in":
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


OUTCOME_RANK = {"rejected": 0, "approximate": 1, "verified": 2}
PRECISION_RANK = {"CITY": 1, "AREA": 2, "STREET": 3}


def _join_address(parts):
    return ", ".join(str(part).strip() for part in parts
                     if str(part or "").strip())


def area_query(expected):
    """Query for the centre of the extracted address's area."""
    expected = expected or {}
    pin = pin_codes.normalize_pin(expected.get("postal_code"))
    return _join_address([pin or expected.get("postal_code"),
                          expected.get("city"), expected.get("state"),
                          expected.get("country")])


def landmark_query(expected):
    """A building/landmark + city query from the noisy address lines.

    Long full addresses often make Geoapify latch onto a random street with
    the same PIN. The building name alone is the strongest signal in that
    case ("Mani Casadona, Kolkata 700160").
    """
    expected = expected or {}
    pin = pin_codes.normalize_pin(expected.get("postal_code"))
    segments = []
    for key in ("address_line2", "address_line1"):
        segments.extend(str(expected.get(key) or "").split(","))
    generic = re.compile(
        r"\b(unit|plot|flat|floor|tower|block|building|phase|sector|area|"
        r"road|street|lane|post|nagar|colony)\b", re.I)
    landmark = ""
    for segment in segments:
        text = " ".join(segment.split()).strip(" .-/")
        if len(text) < 4 or generic.search(text):
            continue
        landmark = text
        break
    if not landmark:
        return ""
    return _join_address([landmark, expected.get("city"),
                          expected.get("state"), pin or expected.get("postal_code"),
                          expected.get("country")])


def lookup_queries(expected):
    """Fallback chain: full address, street + postcode, landmark, area + city."""
    expected = expected or {}
    pin = pin_codes.normalize_pin(expected.get("postal_code"))
    postal = pin or expected.get("postal_code")
    candidates = [
        ("full", _join_address([
            expected.get("address_line1"), expected.get("address_line2"),
            expected.get("city"), expected.get("state"),
            postal, expected.get("country")])),
        ("street_postcode", _join_address([
            expected.get("address_line1"), expected.get("address_line2"),
            postal])),
        ("landmark_city", landmark_query(expected)),
        ("area_city", _join_address([
            expected.get("city"), expected.get("state"),
            postal, expected.get("country")])),
    ]
    out, seen = [], set()
    for kind, query in candidates:
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            out.append((kind, query))
    return out


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def _confidence(result):
    try:
        return float((result or {}).get("rank", {}).get("confidence", 0))
    except (TypeError, ValueError):
        return 0.0


_TRUSTED_SOURCES = {"OWN_SITE", "REGISTRY"}
_HARD_CHECKS = {"country", "expected_state", "city", "postcode",
                "postcode_valid", "area_distance"}
# A contradiction with the contact's own card is hard for a directory, but a
# trusted page may describe a real branch in another state. Those are saved as
# approximate + review, never verified.
_CONTACT_CHECKS = {"state"}


def assess(result, company_location=None, expected=None, *, area=None,
           source_type=None, country_code="in"):
    """Classify a geocode result as verified / approximate / rejected.

    Hard contradictions (wrong country/state/city/PIN, invalid PIN, >25 km)
    reject the result. That is the fix for the old behaviour where a candidate
    with four failed checks still became APPROXIMATE. Soft failures (low
    confidence, weak match type, street wording, area-level pin) may still be
    saved, but only as APPROXIMATE and with the real pin precision recorded.
    """
    checks = check_all(result, company_location, expected,
                       country_code=country_code)
    distance = None
    if result and area:
        try:
            distance = haversine_km(area[0], area[1],
                                    result.get("lat"), result.get("lon"))
        except (TypeError, ValueError):
            distance = None
        if distance is not None:
            checks.append({
                "name": "area_distance",
                "passed": distance <= APPROXIMATE_KM,
                "detail": ("%.1f km from area (verified <= %.1f km, "
                           "approximate <= %.0f km, street <= %.0f km)")
                          % (distance, VERIFIED_KM, APPROXIMATE_KM,
                             STREET_AREA_KM),
                "reason": "area_mismatch",
            })

    failed = [item for item in checks if not item.get("passed")]
    failed_names = {item.get("name") for item in failed}
    failed_reasons = [item.get("reason") for item in failed]
    first_reason = failed[0].get("reason") if failed else "ok"
    has_street = bool(_normalize((expected or {}).get("address_line1")))
    precision = result_precision(result)
    trusted = source_type in _TRUSTED_SOURCES
    verdict = {
        "outcome": "rejected",
        "reason": first_reason,
        "checks": checks,
        "distance_km": distance,
        "confidence": _confidence(result),
        "result": result,
        "pin_precision": precision,
        "review_reasons": [],
    }

    if not result:
        if source_type == "REGISTRY" and has_street:
            verdict.update(outcome="approximate", reason="trusted_source",
                           review_reasons=["trusted_source_without_map"])
        return verdict

    if "country" in failed_names:
        return verdict

    hard = failed_names & (_HARD_CHECKS - {"area_distance"})
    area_failed = "area_distance" in failed_names
    # A confident street-level match is stronger evidence than a coarse PIN
    # centroid, which can itself be wrong (for example 700160). Give it more
    # room before calling it the wrong area, but still reject a genuinely far
    # result and all area/city-level results beyond the normal radius.
    if area_failed and distance is not None:
        strong_text = not (failed_names & {"postcode", "postcode_valid",
                                           "city", "expected_state", "street"})
        limit = STREET_AREA_KM if (precision == "STREET" and strong_text) \
            else APPROXIMATE_KM
        if distance > limit:
            verdict["reason"] = "area_mismatch"
            return verdict

    contact = failed_names & _CONTACT_CHECKS

    if hard:
        # A trusted source does not excuse an internally inconsistent address
        # (wrong PIN/city/state or invalid PIN). It only excuses a mismatch
        # with the contact's own location hint.
        verdict["reason"] = next(
            (item["reason"] for item in failed if item["name"] in hard),
            first_reason)
        return verdict

    if contact and not trusted:
        verdict["reason"] = next(
            (item["reason"] for item in failed if item["name"] in contact),
            first_reason)
        return verdict

    if contact and trusted:
        verdict.update(
            outcome="approximate",
            reason=next((item["reason"] for item in failed
                         if item["name"] in contact), first_reason),
            review_reasons=sorted({reason for name, reason in
                                   ((item["name"], item["reason"])
                                    for item in failed)
                                   if name in contact}))
        return verdict

    essentials = {"street_level", "match_type", "confidence",
                  "street_present", "street"}
    if distance is None:
        # No area centre available: fall back to the postcode/city text checks,
        # which are hard checks already handled above.
        essentials |= {"postcode", "city"}
    if (precision == "STREET"
            and not (failed_names & essentials)
            and (distance is None or distance <= VERIFIED_KM)):
        verdict.update(outcome="verified", reason="ok")
        return verdict

    if has_street:
        verdict["outcome"] = "approximate"
        return verdict
    return verdict


def best_candidate(verdicts):
    """Pick the strongest result across a lookup's up-to-5 candidates.

    Precision sits before distance on purpose: a city centre 0 m from the area
    anchor must not beat a real street pin 2 km away. That ordering bug was
    why so many saves ended up using the area_city lookup.
    """
    if not verdicts:
        return None

    def score(verdict):
        distance = verdict.get("distance_km")
        return (
            OUTCOME_RANK.get(verdict.get("outcome"), 0),
            PRECISION_RANK.get(verdict.get("pin_precision"), 0),
            -(distance if distance is not None else 1e9),
            verdict.get("confidence") or 0,
        )

    return max(verdicts, key=score)


def result_summary(result):
    """Flatten a Geoapify result into the fields the attempt record prints."""
    if not result:
        return None
    rank = result.get("rank") or {}
    postcode = result.get("postcode") or ""
    return {
        "formatted": result.get("formatted") or "",
        "lat": result.get("lat"),
        "lon": result.get("lon"),
        "resultType": result.get("result_type") or "",
        "matchType": rank.get("match_type") or "",
        "confidence": rank.get("confidence"),
        "placeId": result.get("place_id") or "",
        "postcode": postcode,
        "street": result.get("street") or "",
        "precision": result_precision(result),
        "postcodeValid": pin_codes.is_valid(postcode) if postcode else None,
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
