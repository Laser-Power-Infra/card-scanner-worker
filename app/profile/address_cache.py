"""TEMPORARY local JSON cache for the address overhaul.

STATUS
    This module is intentionally disposable. It exists only while the address
    pass is being reworked (filter, field derivation, hard fails, pin
    precision, multi-location). Delete it — plus its call sites, config keys,
    tests and ``data/address_cache/`` — once the overhaul is signed off.

WHY
    Reruns were not comparable: search providers rotate, cooldowns expire, and
    live sites change. Freezing pages/search results for a short window lets a
    before/after run be compared on the same inputs. It also lets a company
    that already failed once in a run be reused instead of re-fetched.

SCOPE / LIMITS
    * Local JSON only. No database table, no migration, no cross-machine
      guarantees.
    * Address pass only.
    * No raw HTML: cleaned page text, title, URL and links, capped at five
      pages per company.
    * No file locking. Two processes may clobber each other's write; that is
      acceptable because the cache is disposable and TTL-bound.
    * Page text may contain emails/phones. ``data/address_cache/`` is
      gitignored and must be deleted at the end of the overhaul.

WHAT IS CACHED
    pages      cleaned pages + search URLs/provider, TTL 72h.
    failure    deterministic failure reason, TTL 12h. Transient failures
               (budget, rate limit, network) are never reused.
    geocode    the full Geoapify result, including ``result_type``,
               ``match_type``, ``confidence`` and derived precision. The
               database LocationCache has no precision columns, so a cached
               pin must carry its own metadata or a city-level pin could be
               mistaken for a street-level one.

KEY
    ``sha1(normalised company + "|" + host(website))[:16]``. Company-only when
    there is no website. Location is deliberately excluded so two contacts at
    the same company share the fetch.

DISABLE / REMOVE
    ``ADDRESS_CACHE=0`` or ``--no-cache``. Grep for ``TEMPORARY CACHE`` to find
    every integration point.
"""
import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import config

log = logging.getLogger("card-desk.address-cache")

# TEMPORARY CACHE — DELETE WITH app/profile/address_cache.py
_MARKER = "TEMPORARY CACHE"
# Bump when the cached shape or the address logic changes during the overhaul.
# Old entries then miss instead of silently feeding stale results into a rerun.
_VERSION = "address-overhaul-2"

_REUSABLE_FAILURES = {
    "no_pages",
    "incomplete_extraction",
    "no_street_line",
    "invalid_postcode",
    "quote_not_found",
    "postcode_mismatch",
    "state_mismatch",
    "city_mismatch",
    "country_mismatch",
    "not_street_level",
    "no_results",
    "low_confidence",
    "street_mismatch",
}

_TRANSIENT_FAILURES = {
    "budget_exhausted",
    "rate_limit",
    "geocode_error",
    "unhandled_exception",
    "schema_missing",
    "no_company",
}

_lock = threading.Lock()


def enabled():
    # Read the environment at call time so tests and --no-cache can disable the
    # cache without reloading config.
    return os.environ.get("ADDRESS_CACHE", config.ADDRESS_CACHE or "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _cache_dir():
    path = Path(config.ADDRESS_CACHE_DIR or "data/address_cache")
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _parse(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def _key(company, website=""):
    host = ""
    raw = str(website or "").strip().lower()
    if raw:
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
    text = "%s|%s" % (re.sub(r"[^a-z0-9]+", " ", str(company or "").lower()).strip(),
                      host)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _path(key):
    return _cache_dir() / ("%s.json" % key)


def _read(key):
    path = _path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.info("address cache unreadable (%s): %s", path.name, exc)
        return None
    return data if isinstance(data, dict) else None


def _write(key, data):
    if not enabled():
        return
    directory = _cache_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = _path(key)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        log.info("address cache write failed: %s", exc)


def _fresh(section, field="expires_at"):
    expires = _parse((section or {}).get(field))
    return bool(expires and expires > _now())


def _base(company, website):
    return {
        "version": _VERSION,
        "key": _key(company, website),
        "company": str(company or ""),
        "website": str(website or ""),
        "updated_at": _iso(_now()),
    }


def _current(data):
    return bool(data) and data.get("version") == _VERSION


def load_company(company, website=""):
    """Return a fresh page bundle or None. A miss/stale entry is normal."""
    if not enabled():
        return None
    key = _key(company, website)
    data = _read(key)
    if not _current(data) or not _fresh(data.get("pages")):
        return None
    pages = data["pages"].get("items") or []
    if not pages:
        return None
    log.info("address cache hit for %s (%s)", company,
             data["pages"].get("fetched_at") or "?")
    return {
        "pages": pages,
        "search_urls": data["pages"].get("search_urls") or [],
        "fetched_at": data["pages"].get("fetched_at"),
    }


def save_company(company, website, pages, search_urls=None):
    """Store cleaned pages for reruns. Never stores raw HTML."""
    if not enabled():
        return
    key = _key(company, website)
    existing = _read(key)
    data = existing if _current(existing) else _base(company, website)
    now = _now()
    data["pages"] = {
        "items": [{
            "url": str(page.get("url") or ""),
            "title": str(page.get("title") or ""),
            "text": str(page.get("text") or "")[:120000],
            "links": list(page.get("links") or [])[:30],
            "company_trust": page.get("company_trust") or "",
            "company_tier": page.get("company_tier") or "",
            "search_result": bool(page.get("search_result")),
        } for page in (pages or [])[:5] if page.get("text")],
        "search_urls": list(search_urls or [])[:20],
        "fetched_at": _iso(now),
        "expires_at": _iso(now + timedelta(hours=config.ADDRESS_CACHE_TTL_HOURS)),
    }
    data["updated_at"] = _iso(now)
    with _lock:
        _write(key, data)


def load_failure(company, website=""):
    """Return a reusable failure record, or None."""
    if not enabled():
        return None
    data = _read(_key(company, website))
    if not _current(data):
        return None
    failure = (data or {}).get("failure") or {}
    if not _fresh(failure) or failure.get("reason") not in _REUSABLE_FAILURES:
        return None
    return failure


def save_failure(company, website, reason, contact_id=""):
    """Record only deterministic failures; transient failures must retry."""
    if not enabled() or reason in _TRANSIENT_FAILURES:
        return
    if reason not in _REUSABLE_FAILURES:
        return
    key = _key(company, website)
    existing = _read(key)
    data = existing if _current(existing) else _base(company, website)
    now = _now()
    data["failure"] = {
        "reason": str(reason),
        "contact_id": str(contact_id or ""),
        "at": _iso(now),
        "expires_at": _iso(now + timedelta(hours=config.ADDRESS_FAILURE_TTL_HOURS)),
    }
    data["updated_at"] = _iso(now)
    with _lock:
        _write(key, data)


def clear_failure(company, website=""):
    if not enabled():
        return
    key = _key(company, website)
    data = _read(key)
    if not data or "failure" not in data:
        return
    data.pop("failure", None)
    data["updated_at"] = _iso(_now())
    with _lock:
        _write(key, data)


def get_geocode(company, website, address):
    """Return the cached full geocode result for one normalised address."""
    if not enabled() or not address:
        return None
    data = _read(_key(company, website))
    if not _current(data):
        return None
    entry = ((data or {}).get("geocode") or {}).get(address)
    if not _fresh(entry) or not isinstance(entry.get("result"), dict):
        return None
    return entry["result"]


def put_geocode(company, website, address, result):
    """Store the full result so precision survives a cache hit."""
    if not enabled() or not address or not isinstance(result, dict):
        return
    key = _key(company, website)
    existing = _read(key)
    data = existing if _current(existing) else _base(company, website)
    geocode = data.setdefault("geocode", {})
    now = _now()
    geocode[address] = {
        "result": result,
        "at": _iso(now),
        "expires_at": _iso(now + timedelta(hours=config.ADDRESS_CACHE_TTL_HOURS)),
    }
    # Keep the sidecar small; old addresses are disposable anyway.
    if len(geocode) > 12:
        for old in sorted(geocode, key=lambda k: geocode[k].get("at") or "")[:-12]:
            geocode.pop(old, None)
    data["updated_at"] = _iso(now)
    with _lock:
        _write(key, data)


def clear(company=None, website=""):
    """Delete one company file, or the whole temp directory when company is None."""
    directory = _cache_dir()
    try:
        if company:
            _path(_key(company, website)).unlink(missing_ok=True)
            return
        for path in directory.glob("*.json"):
            path.unlink(missing_ok=True)
    except OSError as exc:
        log.info("address cache clear failed: %s", exc)
