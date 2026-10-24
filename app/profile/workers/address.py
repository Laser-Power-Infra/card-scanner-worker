"""Address pass: verify a company's postal address via Geoapify geocoding."""
import logging
import re
from datetime import datetime, timezone

import requests

from ...shared import search as search_mod
from .. import address_ai, budget, config, db, geo

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


def _retrieve(company, company_location, website, official_site):
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
                log.info("search failed for %s (scoped=%s): %s", company, scoped, exc)
    # de-duplicate by url, keep text
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
        out.append("%s\n%s" % (url, text[:12000]))
    return out


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

    if not db.address_schema_ok() or not db.location_cache_schema_ok():
        raise RuntimeError("address schema missing; apply the Prisma migration first")

    company, company_location, website, official_site = _load_contact(contact_id, snap)
    if not company:
        db.mark_address_failed(contact_id)
        log.warning("address: no company for contact %s", contact_id)
        return {"ok": False, "site": "address", "reason": "no_company"}

    pages = _retrieve(company, company_location, website, official_site)
    if not pages:
        db.mark_address_failed(contact_id)
        log.info("address: no pages for %s", company)
        return {"ok": False, "site": "address", "reason": "no_pages"}

    extracted = address_ai.extract_company_address(company, company_location, pages)
    if not extracted or not all(extracted.get(k) for k in ("city", "state", "country")):
        db.mark_address_failed(contact_id)
        log.info("address: incomplete extraction for %s", company)
        return {"ok": False, "site": "address", "reason": "incomplete_extraction",
                "data": extracted}

    normalized = _normalize_address(extracted)
    cached = db.get_location_cache(normalized)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if cached and cached.get("expiresAt") and cached["expiresAt"] > now and cached.get("resolved"):
        fields = {k: extracted[k] for k in
                  ("address_line1", "address_line2", "city", "state",
                   "postal_code", "country")}
        db.update_address(contact_id, fields, verified_at=now)
        if not company_location:
            db.set_company_location_if_empty(contact_id,
                                            "%s, %s" % (extracted["city"], extracted["state"]))
        log.info("address: cache hit for %s", company)
        return {"ok": True, "site": "address", "reason": "cache_hit", **fields}

    if not budget.reserve():
        log.error("address: monthly geocode cap reached; stopping")
        return {"ok": False, "site": "address", "reason": "budget_exhausted"}

    addr = ", ".join(x for x in (extracted["address_line1"], extracted["address_line2"],
                                 extracted["city"], extracted["state"],
                                 extracted["postal_code"], extracted["country"]) if x)
    try:
        result = geo.geocode(addr)
    except geo.GeoRateLimit as exc:
        db.mark_address_failed(contact_id)
        log.warning("address: rate limit for %s: %s", company, exc)
        return {"ok": False, "site": "address", "reason": "rate_limit"}
    except Exception as exc:  # noqa: BLE001
        db.mark_address_failed(contact_id)
        log.warning("address: geocode error for %s: %s", company, exc)
        return {"ok": False, "site": "address", "reason": "geocode_error",
                "error": str(exc)}

    ok, reason = geo.accept(result, company_location, extracted)
    if not ok:
        db.mark_address_failed(contact_id)
        log.info("address: rejected (%s) for %s", reason, company)
        return {"ok": False, "site": "address", "reason": reason}

    fields = geo.extract_fields(result)
    cache_fields = {k: fields[k] for k in ("address_line1", "address_line2", "city",
                                           "state", "postal_code", "country")}
    db.update_address(contact_id, cache_fields, verified_at=now)
    if not company_location:
        db.set_company_location_if_empty(contact_id,
                                        "%s, %s" % (fields["city"], fields["state"]))
    from datetime import timedelta
    expires = now + timedelta(days=30)
    db.upsert_location_cache(
        normalized, addr, fields["latitude"], fields["longitude"],
        fields["place_id"], fields["formatted_address"], fields["country"],
        fields["state"], expires)
    log.info("address: verified for %s -> %s", company, fields["city"])
    return {"ok": True, "site": "address", "reason": "verified", **cache_fields}
