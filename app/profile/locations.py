"""Company-location identity, source trust, and primary selection.

Pure helpers only: the address worker feeds these to db.merge_company_location
and the primary rule is re-run after every new location.
"""
import math
import re
from urllib.parse import urlparse

SOURCE_TRUST = {"OWN_SITE": 3, "REGISTRY": 2, "DIRECTORY": 1, "MIGRATED": 0}
STATUS_TRUST = {"VERIFIED": 2, "APPROXIMATE": 1}
PRECISION_RANK = {"STREET": 3, "AREA": 2, "CITY": 1}

TOLERANCE_M = 200
_EARTH_RADIUS_M = 6371000.0
_ADDRESS_KEYS = ("address_line1", "address_line2", "city", "state",
                 "postal_code", "country")
_REGISTRY_HINTS = ("gst", "mca", "lei", ".gov.in")


def norm(value):
    """Lowercase and strip punctuation so address text can be compared."""
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(text.split())


def _coords(location):
    try:
        lat = float(location.get("latitude"))
        lon = float(location.get("longitude"))
    except (TypeError, ValueError):
        return None
    return (lat, lon)


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    radius = _EARTH_RADIUS_M
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def same_location(first, second, tolerance_m=TOLERANCE_M):
    """Two rows are the same place within ~200 m, or same street + postcode."""
    coords_a, coords_b = _coords(first), _coords(second)
    if coords_a and coords_b:
        if haversine_m(*coords_a, *coords_b) <= tolerance_m:
            return True
    street_a = norm(first.get("address_line1"))
    street_b = norm(second.get("address_line1"))
    pin_a = re.sub(r"\D", "", str(first.get("postal_code") or ""))
    pin_b = re.sub(r"\D", "", str(second.get("postal_code") or ""))
    return bool(street_a and pin_a and street_a == street_b and pin_a == pin_b)


def _host(url):
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = (urlparse(text).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def source_type(evidence_url, website="", official_site=""):
    """Classify the page the address came from: own site / registry / directory."""
    host = _host(evidence_url)
    for base in (website, official_site):
        own = _host(base)
        if own and host and (host == own or host.endswith("." + own)):
            return "OWN_SITE"
    flat = host.replace("-", "")
    if any(hint in flat for hint in _REGISTRY_HINTS):
        return "REGISTRY"
    return "DIRECTORY"


def precision(fields):
    """STREET when there is a street line, AREA for a locality, else CITY."""
    if norm((fields or {}).get("address_line1")):
        return "STREET"
    if norm((fields or {}).get("address_line2")):
        return "AREA"
    return "CITY"


def matches_hint(location, company_location):
    """True when the card's companyLocation text names this location."""
    hint = norm(company_location)
    if not hint:
        return False
    text = norm(" ".join(str(location.get(k) or "") for k in _ADDRESS_KEYS))
    return bool(text) and hint in text


def build_location(fields, *, source_url="", website="", official_site="",
                   status="APPROXIMATE"):
    """Turn extracted/geocoded fields into a CompanyLocation candidate."""
    location = {key: (fields or {}).get(key) or "" for key in _ADDRESS_KEYS}
    location.update({
        "latitude": (fields or {}).get("latitude"),
        "longitude": (fields or {}).get("longitude"),
        "source_url": source_url or "",
        "source_type": source_type(source_url, website, official_site),
        "precision": precision(fields),
        "status": status,
        "evidence_saved": bool(source_url),
        "found_count": 1,
    })
    return location


def merge_details(existing, candidate):
    """Fold a repeat sighting into an existing row without losing detail."""
    merged = {key: existing.get(key) or candidate.get(key)
              for key in _ADDRESS_KEYS}
    merged["latitude"] = (existing.get("latitude")
                          if existing.get("latitude") is not None
                          else candidate.get("latitude"))
    merged["longitude"] = (existing.get("longitude")
                           if existing.get("longitude") is not None
                           else candidate.get("longitude"))

    existing_source = existing.get("source_type") or "MIGRATED"
    candidate_source = candidate.get("source_type") or "DIRECTORY"
    if SOURCE_TRUST.get(candidate_source, 0) > SOURCE_TRUST.get(existing_source, 0):
        merged["source_type"] = candidate_source
        merged["source_url"] = candidate.get("source_url") or existing.get("source_url")
    else:
        merged["source_type"] = existing_source
        merged["source_url"] = existing.get("source_url") or candidate.get("source_url")

    merged["status"] = max(
        (existing.get("status") or "APPROXIMATE",
         candidate.get("status") or "APPROXIMATE"),
        key=lambda value: STATUS_TRUST.get(value, 0))
    merged["precision"] = max(
        (existing.get("precision") or "CITY",
         candidate.get("precision") or "CITY"),
        key=lambda value: PRECISION_RANK.get(value, 0))
    merged["evidence_saved"] = bool(existing.get("evidence_saved")
                                    or candidate.get("evidence_saved"))
    merged["found_count"] = int(existing.get("found_count") or 1) + 1
    return merged


def choose_primary(locations, company_location=None):
    """Card match, then source trust, then times found; status/precision break
    remaining ties so the result is deterministic."""
    if not locations:
        return None

    def score(location):
        return (
            1 if matches_hint(location, company_location) else 0,
            SOURCE_TRUST.get(location.get("source_type"), 0),
            int(location.get("found_count") or 1),
            STATUS_TRUST.get(location.get("status"), 0),
            PRECISION_RANK.get(location.get("precision"), 0),
        )

    return max(locations, key=score)
