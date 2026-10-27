"""Company-location identity, source trust, and primary selection.

Pure helpers only: the address worker feeds these to db.merge_company_location
and the primary rule is re-run after every new location.
"""
import math
import re
from urllib.parse import urlparse

from .address_text import ADDRESS_KEYS as _ADDRESS_KEYS
from .address_text import clean_address_fields
from .address_text import norm
from .geo import PRECISION_RANK

SOURCE_TRUST = {"OWN_SITE": 3, "REGISTRY": 2, "DIRECTORY": 1, "MIGRATED": 0}
STATUS_TRUST = {"VERIFIED": 2, "APPROXIMATE": 1}

TOLERANCE_M = 200
_EARTH_RADIUS_M = 6371000.0
_REGISTRY_HINTS = ("gst", "mca", "lei", ".gov.in")
_TRUSTED_SOURCES = {"OWN_SITE", "REGISTRY"}


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
    haystack = ("%s %s" % (host, str(evidence_url or "").lower())).replace("-", "")
    if any(hint in haystack for hint in _REGISTRY_HINTS):
        return "REGISTRY"
    return "DIRECTORY"


def precision(fields):
    """Fallback pin precision when no geocoder verdict is available.

    An extracted street line is not proof that the saved pin is street-level,
    so the fallback is never better than AREA.
    """
    if norm((fields or {}).get("address_line1")) or norm((fields or {}).get("address_line2")):
        return "AREA"
    return "CITY"


def hint_rank(location, company_location):
    """Strength of the match between a location and the contact's own hint.

    3 = city matches, 2 = state matches, 1 = a word overlaps, 0 = no match.
    This is what keeps an out-of-state office from becoming primary when an
    in-state office exists.
    """
    hint = norm(company_location)
    if not hint:
        return 0
    city = norm((location or {}).get("city"))
    state = norm((location or {}).get("state"))
    if city and (city in hint or hint in city):
        return 3
    if state and (state in hint or hint in state):
        return 2
    text = norm(" ".join(str((location or {}).get(k) or "")
                         for k in _ADDRESS_KEYS))
    tokens = [token for token in hint.split() if len(token) >= 4]
    return 1 if any(token in text for token in tokens) else 0


def build_location(fields, *, source_url="", website="", official_site="",
                   status="APPROXIMATE", pin_precision=None):
    """Turn extracted/geocoded fields into a CompanyLocation candidate.

    ``BUILDING`` is the new internal precision, but the database enum only has
    STREET/AREA/CITY. Persist a building pin as STREET and keep the distinction
    on the non-writable ``_precision_internal`` key until the enum is migrated.
    """
    location = {key: (fields or {}).get(key) or "" for key in _ADDRESS_KEYS}
    internal = pin_precision or precision(fields)
    stored = "STREET" if internal == "BUILDING" else internal
    location.update({
        "latitude": (fields or {}).get("latitude"),
        "longitude": (fields or {}).get("longitude"),
        "source_url": source_url or "",
        "source_urls": [source_url] if source_url else [],
        "source_type": source_type(source_url, website, official_site),
        "precision": stored,
        "status": status,
        "evidence_saved": bool(source_url),
        "found_count": 1,
    })
    location["_precision_internal"] = internal
    return location


def distinct_sources(urls):
    """Distinct non-empty source hosts, so two pages on one site count once."""
    return {_host(url) for url in (urls or []) if _host(url)}


def merge_details(existing, candidate):
    """Fold a repeat sighting into an existing row without losing detail."""
    existing = clean_address_fields(existing)
    candidate = clean_address_fields(candidate)
    # The newest quote-validated extraction wins for address parts; the map
    # only ever contributed coordinates. Empty means the new source had no
    # second line, so do not resurrect an older one.
    merged = {key: candidate.get(key) or "" for key in _ADDRESS_KEYS}
    existing_precision = existing.get("precision") or "CITY"
    candidate_precision = candidate.get("precision") or "CITY"
    existing_internal = (existing.get("_precision_internal")
                         or existing_precision)
    candidate_internal = (candidate.get("_precision_internal")
                          or candidate_precision)
    existing_rank = PRECISION_RANK.get(existing_internal, 0)
    candidate_rank = PRECISION_RANK.get(candidate_internal, 0)
    existing_source = existing.get("source_type") or "MIGRATED"
    candidate_source = candidate.get("source_type") or "DIRECTORY"
    # A better pin must replace a coarse one; otherwise a later run can never
    # improve a city/area centroid into the street-level match it just found.
    take_candidate_pin = (
        candidate.get("latitude") is not None
        and (existing.get("latitude") is None
             or candidate_rank > existing_rank
             or (candidate_rank == existing_rank
                 and SOURCE_TRUST.get(candidate_source, 0)
                 > SOURCE_TRUST.get(existing_source, 0))))
    if take_candidate_pin:
        merged["latitude"] = candidate.get("latitude")
        merged["longitude"] = candidate.get("longitude")
    else:
        merged["latitude"] = (existing.get("latitude")
                              if existing.get("latitude") is not None
                              else candidate.get("latitude"))
        merged["longitude"] = (existing.get("longitude")
                               if existing.get("longitude") is not None
                               else candidate.get("longitude"))

    urls = []
    for url in ((existing.get("source_urls") or [])
                + [existing.get("source_url"), candidate.get("source_url")]):
        if url and url not in urls:
            urls.append(url)
    merged["source_urls"] = urls

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
    # Precision describes the pin that was actually kept: a candidate that only
    # claims STREET while leaving the old city coordinates in place must not
    # relabel the row.
    merged["precision"] = (candidate_precision if take_candidate_pin
                           else existing_precision)
    kept_internal = (candidate_internal if take_candidate_pin
                     else existing_internal)
    merged["_precision_internal"] = kept_internal
    merged["evidence_saved"] = bool(existing.get("evidence_saved")
                                    or candidate.get("evidence_saved"))
    merged["found_count"] = int(existing.get("found_count") or 1) + 1

    # Two independent sites agreeing upgrade an approximate row, but only when
    # at least one of them is trusted (own site or registry), they agree on the
    # PIN, and the kept pin is actually building-level. Two directories copying
    # each other are not verification, and a street/area pin is not either.
    trusted = (existing_source in _TRUSTED_SOURCES
               or candidate_source in _TRUSTED_SOURCES)
    pins = {re.sub(r"\D", "", str(row.get("postal_code") or ""))
            for row in (existing, candidate)}
    pins.discard("")
    if (trusted and len(distinct_sources(urls)) >= 2 and len(pins) <= 1
            and kept_internal == "BUILDING"
            and merged["status"] != "VERIFIED"):
        merged["status"] = "VERIFIED"
    # A VERIFIED label is only ever valid for a BUILDING pin. This also
    # downgrades rows saved before the rule existed when they are next merged.
    if merged["status"] == "VERIFIED" and kept_internal != "BUILDING":
        merged["status"] = "APPROXIMATE"
    return merged


def choose_primary(locations, company_location=None):
    """Card match, then status/precision, then source trust and sightings.

    The card hint stays first so a real branch in the contact's own state wins
    over a head office in another state. Precision then keeps a street pin
    ahead of a city centroid when both are otherwise equal.
    """
    if not locations:
        return None

    def score(location):
        return (
            hint_rank(location, company_location),
            STATUS_TRUST.get(location.get("status"), 0),
            PRECISION_RANK.get(location.get("precision"), 0),
            SOURCE_TRUST.get(location.get("source_type"), 0),
            int(location.get("found_count") or 1),
        )

    return max(locations, key=score)
