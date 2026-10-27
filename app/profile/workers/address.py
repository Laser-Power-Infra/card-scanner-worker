"""Address pass: verify a company's postal address via Geoapify geocoding."""
import logging
from datetime import datetime, timedelta, timezone

import requests

from ...shared import search as search_mod
from .. import (address_ai, address_attempt, address_cache, address_fields,
                budget, config, db, geo, locations, page_quality, pin_codes)

log = logging.getLogger("card-desk.worker.address")


def _normalize_address(fields):
    parts = [fields.get("address_line1"), fields.get("address_line2"),
             fields.get("city"), fields.get("state"),
             fields.get("postal_code"), fields.get("country")]
    return " ".join(" ".join(str(p or "").lower().split()) for p in parts).strip()


_CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/about",
                  "/about-us", "/reach-us", "/locations")
_MAX_FETCH_PAGES = 8
_MAX_LLM_PAGES = 5

# TEMPORARY CACHE — remove with app/profile/address_cache.py
# Pages/search/geocode metadata are frozen locally for repeatable reruns.
# This is deliberately not a database cache and has no cross-machine promise.


def _fetch_page(url):
    """Fetch one HTML page, returning cleaned text and same-site links."""
    try:
        response = requests.get(url, timeout=config.SCRAPE_TIMEOUT,
                                headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        log.info("fetch failed for %s: %s", url, exc)
        return None
    if not response.ok:
        return None
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "text/plain" not in content_type:
        return None
    if response.encoding in (None, "ISO-8859-1"):
        response.encoding = response.apparent_encoding or "utf-8"
    html = response.text[:1000000]
    text = page_quality.html_to_text(html)
    if not text:
        return None
    from urllib.parse import urlparse
    return {
        "title": urlparse(response.url).path or "/",
        "url": response.url,
        "text": text,
        "links": page_quality.extract_links(html, response.url),
    }


def _fetch_xml(url):
    try:
        response = requests.get(url, timeout=config.SCRAPE_TIMEOUT,
                                headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        log.info("sitemap fetch failed for %s: %s", url, exc)
        return ""
    if not response.ok:
        return ""
    return response.text[:500000]


def _sitemap_links(root):
    """Contact-like URLs from sitemap.xml, following one index level."""
    urls = []
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        xml = _fetch_xml(root.rstrip("/") + path)
        if xml:
            found = page_quality.parse_sitemap(xml)
            if found:
                urls = found
                break
    for child in [url for url in urls if url.endswith(".xml")][:2]:
        urls.extend(page_quality.parse_sitemap(_fetch_xml(child)))
    return [url for url in urls
            if page_quality.is_page_candidate_url(url)][:12]


def _roots(website, official_site):
    roots = []
    for base in (website, official_site):
        host = search_mod._direct_domain([base]) if base else ""
        if host and " " not in host:
            root = "https://" + host
            if root not in roots:
                roots.append(root)
    return roots


def _fetch_site(root):
    """Homepage + discovered contact/about/location pages for one site."""
    from urllib.parse import urljoin

    home = _fetch_page(root)
    pages = [home] if home else []
    discovered = list((home or {}).get("links") or [])
    discovered.extend(_sitemap_links(root))
    if not discovered:
        discovered.extend(urljoin(root.rstrip("/") + "/", path.lstrip("/"))
                          for path in _CONTACT_PATHS)

    seen = {page_quality.url_key(page.get("url")) for page in pages}
    for url in discovered:
        if len(pages) >= _MAX_FETCH_PAGES:
            break
        key = page_quality.url_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        page = _fetch_page(url)
        if page:
            pages.append(page)
    return pages


def _filter_pages(pages, company, company_location, website, notes):
    """Keep high/medium company matches; fall back to best low-trust pages."""
    if not pages:
        return []
    matched, tiers = [], {"high": 0, "medium": 0, "low": 0, "none": 0}
    for page in pages:
        tier, reasons = page_quality.company_match(
            page, company, company_location, website)
        page["company_tier"] = tier
        page["company_reasons"] = reasons
        tiers[tier] = tiers.get(tier, 0) + 1
        if tier in ("high", "medium"):
            matched.append(page)
    notes.append("company_match: high=%d medium=%d low=%d none=%d"
                 % (tiers.get("high", 0), tiers.get("medium", 0),
                    tiers.get("low", 0), tiers.get("none", 0)))
    if matched:
        return matched

    # The old behaviour returned [] here and failed company_mismatch. Keep the
    # best pages instead, mark them low-trust, and let the address gate decide.
    for page in pages:
        page["company_trust"] = "low"
    notes.append("company_filter_fallback: no high/medium match; "
                 "keeping best pages as low-trust")
    return pages


def _search_pages(company, company_location, roots, notes):
    """Unscoped search first, scoped fallback. Marks results for reuse."""
    query = "%s registered office address %s" % (
        company, company_location or "India")
    for scoped in (False, True):
        try:
            results = search_mod.search(
                query, domains=roots if scoped else None)
            if results:
                for result in results:
                    result["search_result"] = True
                notes.append("search returned %d results (scoped=%s)"
                             % (len(results), scoped))
                return results
        except Exception as exc:  # noqa: BLE001 - best effort
            notes.append("search failed for %s (scoped=%s): %s"
                         % (company, scoped, exc))
            log.info("search failed for %s (scoped=%s): %s",
                     company, scoped, exc)
    return []


def _retrieve(company, company_location, website, official_site, notes=None):
    """Fetch and rank candidate pages. Returns the best 5 for extraction."""
    notes = notes if notes is not None else []

    # TEMPORARY CACHE: return frozen pages for a rerun. Remove with the module.
    cached = address_cache.load_company(company, website)
    if cached:
        notes.append("cache hit: pages=%d fetched=%s"
                     % (len(cached.get("pages") or []),
                        cached.get("fetched_at") or "?"))
        return list(cached.get("pages") or [])

    roots = _roots(website, official_site)
    pages = []
    for root in roots:
        pages.extend(_fetch_site(root))

    # Search when no site page contains a real street address. A bare
    # "Head Office, Hyderabad 500081" is not enough: the site may be
    # JavaScript-rendered and only directories carry the street line.
    if not any(page_quality.has_street_address(page.get("text", ""))
               for page in pages):
        notes.append("no site page with a street address; unscoped search")
        pages.extend(_search_pages(company, company_location, roots, notes))

    filtered = _filter_pages(pages, company, company_location, website, notes)
    selected = page_quality.select_pages(
        filtered, company, company_location, limit=_MAX_LLM_PAGES)
    if not selected and pages:
        # select_pages can drop everything as thin/dead. Keep the best raw page
        # only when the filter had already accepted it.
        selected = page_quality.select_pages(
            pages, company, company_location, limit=1)
        for page in selected:
            page["company_trust"] = "low"
        if selected:
            notes.append("company_filter_fallback: thin pages kept low-trust")
    notes.append("pages fetched=%d selected=%d dropped=%d" % (
        len(pages), len(selected), max(0, len(pages) - len(selected))))

    # TEMPORARY CACHE: store cleaned pages for the next rerun.
    if selected:
        address_cache.save_company(company, website, selected)
    return selected


def _pages_for_ai(pages):
    return ["%s\n%s" % (page.get("url", ""),
                        page_quality.condense(page.get("text", "")))
            for page in pages]


_TYPE_RANK = {"head_office": 0, "registered": 1, "branch": 2, "other": 3}
_PERMANENT_REASONS = {"country_mismatch"}


def _quote_page(address, pages):
    quote = address.get("quote") or ""
    for page in pages:
        if page_quality.quote_in_page(quote, page.get("text", "")):
            return page
    return None


def _valid_addresses(raw_addresses, pages, country_code="in",
                     company_location=""):
    """PO-box strip, field derivation, PIN validation, quote validation.

    Returns ``(valid, rejected)`` where valid entries are ordinary field dicts
    plus private keys prefixed with ``_`` for provenance.
    """
    valid, rejected = [], []
    india = (country_code or "in").lower() == "in"
    for raw in raw_addresses or []:
        cleaned = address_ai.strip_po_box(raw)
        page = _quote_page(cleaned, pages)
        prepared = address_fields.fill(
            cleaned,
            company_location=company_location,
            page_text=(page or {}).get("text", ""),
            quote=cleaned.get("quote") or "",
            india=india,
        )
        fields = prepared["fields"]
        if not locations.norm(fields.get("address_line1")):
            rejected.append((fields, "no_street_line"))
            continue
        if any(conflict.startswith("invalid_pin:")
               for conflict in prepared["conflicts"]):
            rejected.append((fields, "invalid_postcode"))
            continue
        if not address_fields.deliverable(fields, india=india):
            rejected.append((fields, "incomplete"))
            continue
        if (india and fields.get("country")
                and "india" not in fields["country"].lower()):
            rejected.append((fields, "country_mismatch"))
            continue
        if page is None:
            rejected.append((fields, "quote_not_found"))
            continue
        fields["evidence_url"] = page.get("url") or cleaned.get("evidence_url") or ""
        fields["address_type"] = cleaned.get("address_type") or "other"
        fields["quote"] = cleaned.get("quote") or ""
        fields["_derived"] = prepared["derived"]
        fields["_pin"] = prepared["pin"]
        valid.append(fields)
    return valid, rejected


def _pick_address(valid, website, official_site, company_location=""):
    """Pick the primary candidate: card-hint first, then office type."""
    def rank(address):
        source = locations.source_type(
            address.get("evidence_url") or "", website, official_site)
        return (-locations.hint_rank(address, company_location),
                _TYPE_RANK.get(address.get("address_type"), 3),
                -locations.SOURCE_TRUST.get(source, 0),
                -len(address.get("quote") or ""))

    return min(valid, key=rank)


def _note_candidates(attempt, valid, rejected, chosen=None):
    attempt["notes"].append("address candidates: valid=%d rejected=%d"
                            % (len(valid), len(rejected)))
    for address, reason in rejected:
        attempt["notes"].append(
            "rejected address (%s): %s %s" % (
                reason, address.get("address_type") or "other",
                address.get("address_line1") or "-"))
    for address in valid:
        if address is chosen:
            continue
        attempt["notes"].append(
            "other address: %s %s" % (
                address.get("address_type") or "other",
                address.get("address_line1") or "-"))


def _save_location(contact_id, fields, extracted, website, official_site,
                   company_location, status="VERIFIED", pin_precision=None,
                   low_trust=False):
    """Add/merge the geocoded result as a CompanyLocation and return primary."""
    if low_trust and status == "VERIFIED":
        status = "APPROXIMATE"
    candidate = locations.build_location(
        fields,
        source_url=(extracted or {}).get("evidence_url") or "",
        website=website, official_site=official_site, status=status,
        pin_precision=pin_precision)
    primary = db.merge_company_location(contact_id, candidate, company_location)
    if (primary and not company_location and primary.get("city")
            and not low_trust):
        db.set_company_location_if_empty(
            contact_id, "%s, %s" % (primary["city"], primary.get("state") or ""))
    return primary


def _area_point(extracted, notes, country_filter="in"):
    """Coordinates used to judge distance from the address's own area.

    A valid PIN already carries a centroid in the local GeoNames table, so the
    common case costs zero Geoapify requests. Fall back to the cached area
    lookup only when the PIN is missing/unknown.
    """
    pin = pin_codes.lookup(extracted.get("postal_code"))
    if pin["valid"] and pin["lat"] is not None and pin["lon"] is not None:
        notes.append("area from pin %s (%s)" % (pin["pin"], pin["city"] or "-"))
        return (pin["lat"], pin["lon"])

    query = geo.area_query(extracted)
    if not query:
        return None
    key = " ".join(query.lower().split())
    cached = db.get_location_cache(key)
    if (cached and cached.get("resolved")
            and cached.get("latitude") is not None
            and cached.get("longitude") is not None):
        return (cached["latitude"], cached["longitude"])
    if not budget.reserve():
        notes.append("budget exhausted before area lookup")
        return None
    try:
        results = geo.geocode_many(query, limit=1,
                                   country_filter=country_filter)
    except geo.GeoRateLimit as exc:
        notes.append("area lookup rate limited: %s" % exc)
        return None
    except Exception as exc:  # noqa: BLE001 - area lookup is best effort
        notes.append("area lookup failed: %s" % type(exc).__name__)
        return None
    if not results:
        return None
    result = results[0]
    if result.get("lat") is None or result.get("lon") is None:
        return None
    expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)
    db.upsert_location_cache(
        key, query, result.get("lat"), result.get("lon"),
        result.get("place_id"), result.get("formatted"),
        result.get("country"), result.get("state"), expires)
    return (result.get("lat"), result.get("lon"))


def _fields_for(result, extracted):
    """Keep extracted parts; fill blanks from the map without overwriting."""
    fields = {key: extracted.get(key) or "" for key in
              ("address_line1", "address_line2", "city", "state",
               "postal_code", "country")}
    if result:
        fields["latitude"] = result.get("lat")
        fields["longitude"] = result.get("lon")
        if not fields["city"]:
            fields["city"] = (result.get("city") or result.get("district")
                              or result.get("county") or "")
        if not fields["state"]:
            fields["state"] = result.get("state") or ""
        if not fields["country"]:
            fields["country"] = result.get("country") or ""
        if not fields["postal_code"] and pin_codes.is_valid(result.get("postcode")):
            fields["postal_code"] = pin_codes.normalize_pin(result.get("postcode"))
    return fields


def _status_from(primary, fallback):
    value = (primary or {}).get("status") or fallback
    return "verified" if value == "VERIFIED" else "approximate"


def _cache_fields(fields):
    return {key: fields.get(key) or "" for key in
            ("address_line1", "address_line2", "city", "state",
             "postal_code", "country")}


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


def _address_order(addresses, company_location):
    def rank(address):
        return (-locations.hint_rank(address, company_location),
                _TYPE_RANK.get(address.get("address_type"), 3),
                -len(address.get("quote") or ""))
    return sorted(addresses, key=rank)


def _best_reason(reasons, aborted):
    for reason in reasons:
        if reason and reason not in ("no_results", "low_confidence"):
            return reason
    return reasons[0] if reasons else (aborted or "no_results")


def _process_address(contact_id, company, website, official_site,
                     company_location, extracted, country_filter, low_trust,
                     notes):
    """Geocode/save one extracted address.

    Returns ``(saved_or_None, reason, lookups, verdict)``.
    """
    normalized = _normalize_address(extracted)
    area = _area_point(extracted, notes, country_filter)
    source_type = locations.source_type(
        extracted.get("evidence_url") or "", website, official_site)
    lookups = []
    best = None
    verdict = None
    best_lookup = None

    # TEMPORARY CACHE: full result so pin precision survives a cache hit.
    cached_result = address_cache.get_geocode(company, website, normalized)
    if cached_result:
        verdict = geo.assess(cached_result, company_location, extracted,
                             area=area, source_type=source_type,
                             country_code=country_filter)
        if verdict["outcome"] in ("verified", "approximate"):
            lookups.append({
                "kind": "cache", "query": normalized,
                "results": 1, "best": verdict["outcome"],
                "precision": verdict.get("pin_precision"),
                "distance_km": verdict.get("distance_km"),
                "reason": verdict.get("reason"), "worked": True,
            })
            notes.append("geocode cache hit: %s/%s"
                         % (verdict["outcome"], verdict.get("pin_precision") or "-"))
            best = verdict
            best_lookup = 0
        else:
            notes.append("cached result rejected; doing fresh lookups")

    if best is None:
        aborted = None
        for kind, query in geo.lookup_queries(extracted):
            if not budget.reserve():
                aborted = "budget_exhausted"
                notes.append("budget exhausted before %s lookup" % kind)
                break
            try:
                results = geo.geocode_many(query, limit=5,
                                           country_filter=country_filter)
            except geo.GeoRateLimit as exc:
                aborted = "rate_limit"
                notes.append("%s lookup rate limited: %s" % (kind, exc))
                break
            except Exception as exc:  # noqa: BLE001
                aborted = "geocode_error"
                notes.append("%s lookup failed: %s: %s"
                             % (kind, type(exc).__name__, exc))
                break

            verdicts = [geo.assess(result, company_location, extracted,
                                   area=area, source_type=source_type,
                                   country_code=country_filter)
                        for result in results]
            pick = geo.best_candidate(verdicts)
            lookups.append({
                "kind": kind,
                "query": query,
                "results": len(results),
                "best": pick["outcome"] if pick else "no_results",
                "precision": pick.get("pin_precision") if pick else None,
                "distance_km": pick.get("distance_km") if pick else None,
                "reason": pick.get("reason") if pick else "no_results",
                "worked": False,
            })
            if (pick and pick["outcome"] == "rejected"
                    and pick["reason"] in _PERMANENT_REASONS):
                aborted = pick["reason"]
                notes.append("%s lookup permanent failure: %s"
                             % (kind, pick["reason"]))
                break
            if pick and (best is None or geo.best_candidate([best, pick]) is pick):
                best = pick
                best_lookup = len(lookups) - 1
            if best and best["outcome"] == "verified":
                break

        if best_lookup is not None:
            lookups[best_lookup]["worked"] = True
        if aborted and best is None:
            return None, aborted, lookups, verdict

    if best is None:
        verdict = geo.assess(None, company_location, extracted,
                             area=area, source_type=source_type,
                             country_code=country_filter)
        if verdict["outcome"] == "approximate":
            best = verdict
            notes.append("trusted source saved without a map result")
        else:
            return None, "no_results", lookups, verdict

    if best["outcome"] not in ("verified", "approximate"):
        return None, best.get("reason") or "no_results", lookups, best

    # TEMPORARY CACHE: only accepted results are cached. A rejected candidate
    # must be re-tried on the next run, not frozen for 72 hours.
    if best.get("result"):
        address_cache.put_geocode(company, website, normalized, best["result"])

    fields = _fields_for(best.get("result"), extracted)
    precision = best.get("pin_precision") or locations.precision(fields)
    status = "VERIFIED" if best["outcome"] == "verified" else "APPROXIMATE"
    primary = _save_location(
        contact_id, fields, extracted, website, official_site,
        company_location, status=status, pin_precision=precision,
        low_trust=low_trust)
    if best.get("result"):
        # Keep the database LocationCache warm for the app map. The worker no
        # longer trusts it for the gate: it has no precision metadata.
        expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)
        result = best["result"]
        db.upsert_location_cache(
            normalized, normalized, result.get("lat"), result.get("lon"),
            result.get("place_id"), result.get("formatted"),
            result.get("country"), result.get("state"), expires)
    review = list(best.get("review_reasons") or [])
    if low_trust:
        review.append("low_company_match")
    notes.append("saved %s/%s at %s" % (
        status.lower(), precision, fields.get("city") or "-"))
    return {
        "primary": primary,
        "fields": fields,
        "status": status,
        "precision": precision,
        "review": review,
        "verdict": best,
        "source_type": source_type,
        "extracted": extracted,
    }, "ok", lookups, best


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

        # TEMPORARY CACHE: reuse a deterministic failure within TTL. Transient
        # budget/rate-limit errors are never stored by address_cache.
        cached_failure = address_cache.load_failure(company, website)
        if cached_failure:
            reason = cached_failure.get("reason") or "no_results"
            note = ("reused failure from %s at %s"
                    % (cached_failure.get("contact_id") or "-",
                       cached_failure.get("at") or "-"))
            return finish({"ok": False, "site": "address", "reason": reason},
                          "FAIL", reason, mark_failed=True, extra_notes=[note])

        pages = _retrieve(company, company_location, website, official_site,
                          notes=attempt["notes"])
        attempt["pages"] = [address_attempt.page(p.get("url", ""), p.get("text", ""))
                            for p in pages]
        low_trust = any(page.get("company_trust") == "low" for page in pages)
        if low_trust:
            attempt["review"] = ["low_company_match"]
        if not pages:
            log.info("address: no pages for %s", company)
            address_cache.save_failure(company, website, "no_pages", contact_id)
            return finish({"ok": False, "site": "address", "reason": "no_pages"},
                          "FAIL", "no_pages", mark_failed=True)

        raw_addresses = address_ai.extract_company_addresses(
            company, company_location, _pages_for_ai(pages))
        country_filter = geo.country_filter_for(company_location, company)
        valid, rejected = _valid_addresses(raw_addresses, pages, country_filter,
                                           company_location)

        # A JavaScript-only site can look like it has an address (a street word
        # and a phone-like number) while extraction returns nothing. If the
        # widened search has not run yet, run it now and retry once.
        if not valid and not any(page.get("search_result") for page in pages):
            notes = attempt["notes"]
            more = _search_pages(company, company_location,
                                 _roots(website, official_site), notes)
            if more:
                pages = page_quality.select_pages(
                    pages + more, company, company_location,
                    limit=_MAX_LLM_PAGES)
                attempt["pages"] = [
                    address_attempt.page(p.get("url", ""), p.get("text", ""))
                    for p in pages]
                address_cache.save_company(company, website, pages)
                raw_addresses = address_ai.extract_company_addresses(
                    company, company_location, _pages_for_ai(pages))
                valid, rejected = _valid_addresses(
                    raw_addresses, pages, country_filter, company_location)

        if not valid:
            _note_candidates(attempt, valid, rejected)
            reason = "incomplete_extraction"
            rejected_reasons = {item for _, item in rejected}
            for candidate in ("invalid_postcode", "state_mismatch",
                              "country_mismatch", "no_street_line"):
                if candidate in rejected_reasons:
                    reason = candidate
                    break
            log.info("address: no valid address for %s (%s)", company, reason)
            address_cache.save_failure(company, website, reason, contact_id)
            return finish({"ok": False, "site": "address",
                           "reason": reason,
                           "data": {"addresses": raw_addresses,
                                    "rejected": [{"reason": reason,
                                                  **address}
                                                 for address, reason in rejected]}},
                          "FAIL", reason, mark_failed=True)

        ordered = _address_order(valid, company_location)
        chosen = ordered[0] if ordered else None
        _note_candidates(attempt, valid, rejected, chosen)
        attempt["extracted"] = chosen
        notes = attempt["notes"]
        if country_filter != "in":
            notes.append("international contact: expected country %s"
                         % country_filter)

        saved, reasons, lookups, review = [], [], [], []
        last_verdict = None
        for address in ordered[:max(1, config.ADDRESS_MAX_LOCATIONS)]:
            result, reason, address_lookups, address_verdict = _process_address(
                contact_id, company, website, official_site, company_location,
                address, country_filter, low_trust, notes)
            lookups.extend(address_lookups)
            if address_verdict:
                last_verdict = address_verdict
            if result:
                saved.append(result)
                review.extend(result.get("review") or [])
                if result.get("extracted") is not address:
                    notes.append("saved extra address: %s"
                                 % (address.get("address_line1") or "-"))
            else:
                reasons.append(reason)

        if not saved:
            reason = _best_reason(reasons, "no_results")
            address_cache.save_failure(company, website, reason, contact_id)
            attempt["checks"] = (last_verdict or {}).get("checks") or []
            attempt["map"] = geo.result_summary(
                (last_verdict or {}).get("result"))
            log.info("address: rejected (%s) for %s", reason, company)
            return finish({"ok": False, "site": "address", "reason": reason},
                          "FAIL", reason,
                          mark_failed=reason != "budget_exhausted",
                          lookups=lookups)

        # Choose the primary locally as well as in the database. The DB rule is
        # authoritative, but tests and multi-location saves need the same
        # deterministic choice before the next location is merged.
        local_candidates = [dict(item.get("fields") or {},
                                 status=item.get("status"),
                                 precision=item.get("precision"),
                                 source_type=item.get("source_type"),
                                 found_count=1)
                            for item in saved]
        local_primary = locations.choose_primary(local_candidates,
                                                 company_location)
        matched = saved[0]
        for index, candidate in enumerate(local_candidates):
            if candidate is local_primary:
                matched = saved[index]
                break
        primary = matched.get("primary")
        if primary is None:
            primary = next((item.get("primary") for item in saved
                            if item.get("primary")), None)
        final = _status_from(primary, matched.get("status") or "APPROXIMATE")
        attempt["extracted"] = primary or matched.get("extracted")
        attempt["checks"] = (matched.get("verdict") or {}).get("checks") or []
        attempt["map"] = geo.result_summary(
            (matched.get("verdict") or {}).get("result"))
        if review:
            attempt["review"] = sorted(set(review))
        notes.append("primary location: %s" % ((primary or {}).get("id") or "-"))
        notes.append("saved locations: %d of %d candidates"
                     % (len(saved), len(ordered)))
        address_cache.clear_failure(company, website)
        log.info("address: %s for %s (%d location(s))",
                 final, company, len(saved))
        return finish({"ok": True, "site": "address", "reason": final,
                       "address_status": final,
                       **_cache_fields(matched.get("fields") or {})},
                      "PASS", final, lookups=lookups)
    except Exception as exc:  # noqa: BLE001 - record then re-raise
        address_attempt.note(attempt, "%s: %s" % (type(exc).__name__, exc))
        address_attempt.finish(attempt, "FAIL", "unhandled_exception")
        raise
