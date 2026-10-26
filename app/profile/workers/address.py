"""Address pass: verify a company's postal address via Geoapify geocoding."""
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from ...shared import search as search_mod
from .. import (address_ai, address_attempt, budget, config, db, geo,
                locations)

log = logging.getLogger("card-desk.worker.address")


def _normalize_address(fields):
    parts = [fields.get("address_line1"), fields.get("address_line2"),
             fields.get("city"), fields.get("state"),
             fields.get("postal_code"), fields.get("country")]
    return " ".join(" ".join(str(p or "").lower().split()) for p in parts).strip()


_CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/about",
                  "/about-us", "/reach-us", "/locations")
_ADDRESS_HINT = re.compile(
    r"\b(address|registered office|corporate office|head office|"
    r"headquarter|h\.?q\.?)\b", re.I)


def _fetch_text(url):
    try:
        response = requests.get(url, timeout=config.SCRAPE_TIMEOUT,
                                headers={"User-Agent": config.USER_AGENT})
        if not response.ok or "text/html" not in response.headers.get(
                "content-type", ""):
            return ""
        from html import unescape
        text = unescape(re.sub(r"<[^>]+>", " ", response.text))
        return re.sub(r"\s+", " ", text).strip()[:30000]
    except requests.RequestException as exc:
        log.info("fetch failed for %s: %s", url, exc)
        return ""


def _retrieve(company, company_location, website, official_site, notes=None):
    """Fetch candidate pages. Returns [{title, url, text}], best effort."""
    notes = notes if notes is not None else []
    pages = []
    domains = []
    for base in (website, official_site):
        if base and base not in domains:
            domains.append(base)
    from urllib.parse import urljoin

    for base in list(domains):
        host = search_mod._direct_domain([base])
        if host and " " not in host:
            try:
                pages.extend(search_mod._scrape_domain(host))
            except Exception as exc:  # noqa: BLE001 - best effort
                notes.append("direct scrape failed for %s: %s" % (host, exc))
                log.info("direct scrape failed for %s: %s", host, exc)
        root = (base if "://" in base else "https://" + base).strip()
        if " " in root:
            continue
        for path in _CONTACT_PATHS:
            url = urljoin(root.rstrip("/") + "/", path.lstrip("/"))
            text = _fetch_text(url)
            if text:
                pages.append({"title": path, "url": url, "text": text})
    joined = " ".join((p or {}).get("text", "") for p in pages)
    if not pages or not _ADDRESS_HINT.search(joined):
        query = "%s headquarters address %s" % (company, company_location or "India")
        for scoped in (False, True):
            try:
                pages.extend(search_mod.search(
                    query, domains=domains if scoped else None))
                break
            except Exception as exc:  # noqa: BLE001
                notes.append("search failed for %s (scoped=%s): %s"
                             % (company, scoped, exc))
                log.info("search failed for %s (scoped=%s): %s", company, scoped, exc)
    # de-duplicate by url, keep the full text (the attempt record only needs a
    # snippet; the LLM input is truncated in _pages_for_ai).
    seen = set()
    out = []
    for r in pages:
        url = (r or {}).get("url", "")
        text = (r or {}).get("text", "")
        if not text or len(text) < 80:
            continue
        key = url or text[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": (r or {}).get("title", ""), "url": url, "text": text})
    return out


def _pages_for_ai(pages):
    return ["%s\n%s" % (p.get("url", ""), p.get("text", "")[:12000])
            for p in pages]


def _save_location(contact_id, fields, extracted, website, official_site,
                   company_location, status="VERIFIED"):
    """Add/merge the geocoded result as a CompanyLocation and return the primary."""
    candidate = locations.build_location(
        fields,
        source_url=(extracted or {}).get("evidence_url") or "",
        website=website, official_site=official_site, status=status)
    primary = db.merge_company_location(contact_id, candidate, company_location)
    if primary and not company_location and primary.get("city"):
        db.set_company_location_if_empty(
            contact_id, "%s, %s" % (primary["city"], primary.get("state") or ""))
    return primary


def _load_contact(contact_id, snap):
    company = (snap.get("company") or "").strip()
    company_location = (snap.get("companyLocation") or "").strip()
    website = (snap.get("website") or "").strip()
    official_site = ""
    if not company or not company_location or not website:
        row = db.get_contact(contact_id)
        if row:
            company = company or (row.get("company") or "").strip()
            company_location = company_location or (row.get("companyLocation") or "").strip()
            website = website or (row.get("website") or "").strip()
    enrichment = db.get_enrichment(contact_id) or {}
    official_site = (enrichment.get("official_site") or "").strip()
    return company, company_location, website, official_site


def run(task):
    tag = task.get("tag") or "address"
    if tag != "address":
        return None
    contact_id = task["contactId"]
    snap = task.get("snapshot") or {}

    attempt = address_attempt.start(contact_id)

    def finish(result, outcome, reason, *, mark_failed=False, **kwargs):
        # Log before the DB side effect so a DB error cannot hide the real
        # reason; any exception still propagates to the queue handler.
        address_attempt.finish(attempt, outcome, reason, **kwargs)
        if mark_failed:
            db.mark_address_failed(contact_id)
        return result

    try:
        if not (db.address_schema_ok() and db.location_cache_schema_ok()
                and db.company_location_schema_ok()):
            finish({"ok": False, "site": "address", "reason": "schema_missing"},
                   "FAIL", "schema_missing")
            raise RuntimeError("address schema missing; apply the Prisma migration first")

        company, company_location, website, official_site = _load_contact(
            contact_id, snap)
        attempt["company"] = company
        attempt["companyLocation"] = company_location
        if not company:
            log.warning("address: no company for contact %s", contact_id)
            return finish({"ok": False, "site": "address", "reason": "no_company"},
                          "FAIL", "no_company", mark_failed=True)

        pages = _retrieve(company, company_location, website, official_site,
                          notes=attempt["notes"])
        attempt["pages"] = [address_attempt.page(p.get("url", ""), p.get("text", ""))
                            for p in pages]
        if not pages:
            log.info("address: no pages for %s", company)
            return finish({"ok": False, "site": "address", "reason": "no_pages"},
                          "FAIL", "no_pages", mark_failed=True)

        extracted = address_ai.extract_company_address(
            company, company_location, _pages_for_ai(pages))
        attempt["extracted"] = extracted
        if not extracted or not all(extracted.get(k) for k in ("city", "state", "country")):
            log.info("address: incomplete extraction for %s", company)
            return finish({"ok": False, "site": "address",
                           "reason": "incomplete_extraction", "data": extracted},
                          "FAIL", "incomplete_extraction", mark_failed=True)

        normalized = _normalize_address(extracted)
        cached = db.get_location_cache(normalized)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if cached and cached.get("expiresAt") and cached["expiresAt"] > now and cached.get("resolved"):
            fields = {k: extracted[k] for k in
                      ("address_line1", "address_line2", "city", "state",
                       "postal_code", "country")}
            cached_fields = dict(fields, latitude=cached.get("latitude"),
                                 longitude=cached.get("longitude"))
            primary = _save_location(
                contact_id, cached_fields, extracted, website, official_site,
                company_location)
            address_attempt.note(attempt, "cached geocode result; checks skipped")
            attempt["map"] = {
                "formatted": cached.get("formattedAddress") or "",
                "lat": cached.get("latitude"),
                "lon": cached.get("longitude"),
                "resultType": "cached",
                "matchType": "cache",
                "confidence": None,
                "placeId": cached.get("placeId") or "",
                "postcode": fields.get("postal_code") or "",
                "street": fields.get("address_line1") or "",
            }
            attempt["notes"].append("primary location: %s" % (
                (primary or {}).get("id") or "-"))
            log.info("address: cache hit for %s", company)
            return finish({"ok": True, "site": "address", "reason": "cache_hit",
                           **fields}, "PASS", "cache_hit")

        if not budget.reserve():
            log.error("address: monthly geocode cap reached; stopping")
            return finish({"ok": False, "site": "address",
                           "reason": "budget_exhausted"},
                          "FAIL", "budget_exhausted")

        addr = ", ".join(x for x in (extracted["address_line1"], extracted["address_line2"],
                                     extracted["city"], extracted["state"],
                                     extracted["postal_code"], extracted["country"]) if x)
        try:
            result = geo.geocode(addr)
        except geo.GeoRateLimit as exc:
            log.warning("address: rate limit for %s: %s", company, exc)
            return finish({"ok": False, "site": "address", "reason": "rate_limit"},
                          "FAIL", "rate_limit", mark_failed=True,
                          extra_notes=[str(exc)])
        except Exception as exc:  # noqa: BLE001
            log.warning("address: geocode error for %s: %s", company, exc)
            return finish({"ok": False, "site": "address", "reason": "geocode_error",
                           "error": str(exc)},
                          "FAIL", "geocode_error", mark_failed=True,
                          extra_notes=["%s: %s" % (type(exc).__name__, exc)])

        checks = geo.check_all(result, company_location, extracted)
        attempt["checks"] = checks
        attempt["map"] = geo.result_summary(result)
        ok, reason = geo.decide(checks)
        if not ok:
            log.info("address: rejected (%s) for %s", reason, company)
            return finish({"ok": False, "site": "address", "reason": reason},
                          "FAIL", reason, mark_failed=True)

        fields = geo.extract_fields(result)
        cache_fields = {k: fields[k] for k in ("address_line1", "address_line2", "city",
                                               "state", "postal_code", "country")}
        primary = _save_location(contact_id, fields, extracted, website,
                                 official_site, company_location)
        attempt["notes"].append("primary location: %s" % (
            (primary or {}).get("id") or "-"))
        expires = now + timedelta(days=30)
        db.upsert_location_cache(
            normalized, addr, fields["latitude"], fields["longitude"],
            fields["place_id"], fields["formatted_address"], fields["country"],
            fields["state"], expires)
        log.info("address: verified for %s -> %s", company, fields["city"])
        return finish({"ok": True, "site": "address", "reason": "verified",
                       **cache_fields}, "PASS", "verified")
    except Exception as exc:  # noqa: BLE001 - record then re-raise
        address_attempt.note(attempt, "%s: %s" % (type(exc).__name__, exc))
        address_attempt.finish(attempt, "FAIL", "unhandled_exception")
        raise
