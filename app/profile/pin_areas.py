"""PIN-centred area reference used by the distance gate.

GeoNames delivers one coordinate per PIN, but those coordinates are often the
post-office point for a prefix rather than the PIN's real centre (700101 and
700091 both resolve to Salt Lake in the committed table). The area gate needs a
trustworthy PIN centre, so each PIN is resolved once through Geoapify's postcode
search and cached forever in ``data/pin_areas.json``. When Geoapify has no
answer, the GeoNames coordinate is kept as a labelled fallback.

The cache is a local, disposable file: it is not a database table and carries no
cross-machine promise. Delete it to force a fresh lookup for every PIN.
"""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import geo, india_geo, pin_codes

log = logging.getLogger("card-desk.pin-areas")

_PATH = Path(__file__).resolve().parents[2] / "data" / "pin_areas.json"
_FALLBACK_RETRY = timedelta(days=7)

_lock = threading.Lock()
_data = None


def _path():
    return Path(os.environ.get("PIN_AREAS_FILE") or _PATH)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _load():
    global _data
    if _data is not None:
        return _data
    with _lock:
        if _data is not None:
            return _data
        try:
            raw = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("pins", {})
        _data = raw
        return _data


def _save(data):
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        log.info("PIN area cache write failed: %s", exc)


def _fetched_at(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def _fallback_expired(row):
    """A GeoNames fallback is retried after a week; a Geoapify row never is."""
    if (row or {}).get("source") != "geonames":
        return False
    fetched = _fetched_at((row or {}).get("fetched_at"))
    return fetched is None or _now() - fetched > _FALLBACK_RETRY


def _public(row):
    result = dict(row or {})
    result["valid"] = (result.get("lat") is not None
                       and result.get("lon") is not None)
    return result


def _empty(pin=""):
    return {"pin": pin, "valid": False, "lat": None, "lon": None,
            "city": "", "district": "", "state": "", "source": "",
            "fetched_at": None}


def lookup(pin):
    """Return the cached PIN centre, resolving it through Geoapify once.

    The record shape is stable:
        {"pin", "valid", "lat", "lon", "city", "district", "state",
         "source", "fetched_at"}
    """
    normalized = pin_codes.normalize_pin(pin)
    if not normalized:
        return _empty()
    data = _load()
    row = (data.get("pins") or {}).get(normalized)
    if row and not _fallback_expired(row):
        return _public(row)

    base = pin_codes.lookup(normalized)
    if not base["valid"]:
        return _empty(normalized)

    row = {
        "pin": normalized,
        "lat": base.get("lat"),
        "lon": base.get("lon"),
        "city": base.get("city") or "",
        "district": base.get("district") or "",
        "state": base.get("state") or "",
        "source": "geonames",
        "fetched_at": _iso(_now()),
    }
    try:
        results = geo.geocode_postcode(normalized)
    except Exception as exc:  # noqa: BLE001 - fallback must never break a run
        log.info("PIN %s lookup failed: %s", normalized, type(exc).__name__)
        results = []
    if results:
        result = results[0]
        if result.get("lat") is not None and result.get("lon") is not None:
            row.update({
                "lat": result.get("lat"),
                "lon": result.get("lon"),
                "city": result.get("city") or row["city"],
                "district": result.get("district") or row["district"],
                "state": result.get("state") or row["state"],
                "source": "geoapify",
            })

    with _lock:
        fresh = _load()
        fresh.setdefault("pins", {})[normalized] = row
        fresh["updated_at"] = _iso(_now())
        _save(fresh)
    return _public(row)


def city_matches(pin, city, *extra):
    """True when ``city`` agrees with the PIN's city or district.

    ``city`` can be a whole extracted locality ("New Town, (N) 24 Parganas"),
    so a known city inside it is compared too. An unknown PIN or a record
    without a city is treated as a match: absence of evidence must not clear a
    pin.
    """
    if not pin_codes.normalize_pin(pin) or not city:
        return True
    row = lookup(pin)
    names = [row.get("city"), row.get("district")]
    names.extend(extra)
    known = [name for name in names if name]
    if not known:
        return True
    candidates = [str(city)]
    extracted_city = india_geo.city_from_text(city)
    if extracted_city:
        candidates.append(extracted_city)
    return any(geo.same_city(candidate, name)
               for candidate in candidates for name in known)
