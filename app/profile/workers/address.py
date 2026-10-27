"""Address pass: verify a company's postal address via Geoapify geocoding."""
import ipaddress
import logging
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests

from ...shared import search as search_mod
from .. import (address_ai, address_attempt, address_cache, address_fields,
                config, db, geo, locations, page_quality, pin_areas, pin_codes)
from ..address_text import norm, norm_key, project

log = logging.getLogger("card-desk.worker.address")


def _normalize_address(fields):
    """Cache key for one address; punctuation-insensitive."""
    return norm_key(" ".join(project(fields).values()))


_CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/about",
                  "/about-us", "/reach-us", "/locations")
_MAX_FETCH_PAGES = 8
_MAX_LLM_PAGES = 5
_MAX_PAGE_BYTES = 1_500_000
_MAX_REDIRECTS = 5

# TEMPORARY CACHE — remove with app/profile/address_cache.py
# Pages/search/geocode metadata are frozen locally for repeatable reruns.
# This is deliberately not a database cache and has no cross-machine promise.


def _public_host(url):
    """True when ``url`` is http(s) and its host resolves only to public IPs.

    The URL comes from a contact field, a search result, or an untrusted
    sitemap, so this is the SSRF boundary: no loopback, private, link-local or
    otherwise reserved address, and no local-only hostname.
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal")):
        return False
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


def _get(url, *, binary=False):
    """GET with redirect validation and a hard response-size cap.

    Returns ``(response, body_bytes)`` or ``None``. Redirects are followed
    manually so each hop is re-validated; ``requests`` would otherwise accept a
    redirect into a private address after the first check.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not _public_host(current):
            log.info("blocked fetch of non-public URL %s", current)
            return None
        try:
            response = requests.get(
                current, timeout=config.SCRAPE_TIMEOUT, stream=True,
                allow_redirects=False,
                headers={"User-Agent": config.USER_AGENT})
        except requests.RequestException as exc:
            log.info("fetch failed for %s: %s", current, type(exc).__name__)
            return None
        if response.is_redirect:
            target = urljoin(current, response.headers.get("location") or "")
            response.close()
            if not target:
                return None
            current = target
            continue
        if not response.ok:
            response.close()
            return None
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > _MAX_PAGE_BYTES:
                    break
                chunks.append(chunk)
        except requests.RequestException as exc:
            log.info("fetch read failed for %s: %s", current, type(exc).__name__)
            response.close()
            return None
        finally:
            response.close()
        return response, b"".join(chunks)[:_MAX_PAGE_BYTES]
    return None


def _decode(response, body):
    encoding = response.encoding
    if encoding in (None, "ISO-8859-1"):
        content_type = response.headers.get("content-type", "").lower()
        marker = "charset="
        if marker in content_type:
            encoding = content_type.split(marker, 1)[1].split(";")[0].strip() or "utf-8"
        else:
            encoding = "utf-8"
    try:
        return body.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _fetch_page(url):
    """Fetch one HTML page, returning cleaned text and same-site links."""
    fetched = _get(url)
    if not fetched:
        return None
    response, body = fetched
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "text/plain" not in content_type:
        return None
    html = _decode(response, body)[:1000000]
    text = page_quality.html_to_text(html)
    if not text:
        return None
    return {
        "title": urlparse(response.url).path or "/",
        "url": response.url,
        "text": text,
        "links": page_quality.extract_links(html, response.url),
    }


def _fetch_xml(url):
    fetched = _get(url)
    if not fetched:
        return ""
    response, body = fetched
    return _decode(response, body)[:500000]


def _sitemap_links(root):
    """Contact-like URLs from sitemap.xml, following one index level."""
    root_host = (urlparse(root).hostname or "").lower()
    urls = []
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        xml = _fetch_xml(root.rstrip("/") + path)
        if xml:
            found = page_quality.parse_sitemap(xml)
            if found:
                urls = found
                break

    def same_site(candidate):
        host = (urlparse(candidate).hostname or "").lower()
        return bool(host and root_host
                    and (host == root_host or host.endswith("." + root_host)))

    children = [url for url in urls if url.endswith(".xml") and same_site(url)]
    for child in children[:2]:
        urls.extend(page_quality.parse_sitemap(_fetch_xml(child)))
    return [url for url in urls
            if same_site(url) and page_quality.is_page_candidate_url(url)][:12]


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


def _extract(company, company_location, pages):
    """Extraction result plus a failure kind, if the extractor itself failed.

    "The pages have no address" and "our API key is wrong" must not collapse
    into the same cacheable reason.
    """
    try:
        return address_ai.extract_company_addresses(
            company, company_location, _pages_for_ai(pages),
            raise_on_error=True), None
    except address_ai.ExtractionError as exc:
        log.warning("address extraction failed (%s) for %s", exc.kind, company)
        return [], exc.kind


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
        derived = list(prepared["derived"])

        # A known PIN that names a different city is not a locator for this
        # address. Clear the PIN, keep the street address and let the city
        # centre anchor the distance gate (the address itself is still useful).
        pin_row = prepared.get("pin") or {}
        pin_city_note = ""
        if india and pin_row.get("valid") and fields.get("city"):
            if not pin_areas.city_matches(pin_row["pin"], fields["city"]):
                area = pin_areas.lookup(pin_row["pin"])
                pin_city_note = ("pin %s (%s/%s) does not match city %s"
                                 % (pin_row["pin"], area.get("city") or "-",
                                    area.get("district") or "-",
                                    fields["city"]))
                derived.append("postal_code cleared: " + pin_city_note)
                prepared["conflicts"].append("pin_city_mismatch:%s->%s"
                                             % (fields.get("postal_code"),
                                                pin_city_note))
                fields["postal_code"] = ""
                fields["_pin_city_mismatch"] = pin_city_note

        if not locations.norm(fields.get("address_line1")):
            rejected.append((fields, "no_street_line"))
            continue
        if any(conflict.startswith("invalid_pin:")
               for conflict in prepared["conflicts"]):
            # A bad PIN is a pin problem, not an address problem. Drop it and
            # keep the street line; the city anchor below still locates it.
            raw_pin = next((conflict.split(":", 1)[1]
                            for conflict in prepared["conflicts"]
                            if conflict.startswith("invalid_pin:")), "")
            fields["_invalid_pin"] = raw_pin
            derived.append("invalid pin dropped: %s" % (raw_pin or "-"))
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
        fields["_derived"] = derived
        fields["_pin"] = prepared["pin"]
        valid.append(fields)
    return valid, rejected


def _note_candidates(attempt, valid, rejected, chosen=None):
    attempt["notes"].append("address candidates: valid=%d rejected=%d"
                            % (len(valid), len(rejected)))
    for address in valid:
        if address.get("_pin_city_mismatch"):
            attempt["notes"].append("postal_code cleared: %s"
                                    % address["_pin_city_mismatch"])
        if address.get("_invalid_pin"):
            attempt["notes"].append("invalid pin dropped: %s"
                                    % address["_invalid_pin"])
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
    saved_id = _saved_location_id(primary, candidate)
    if primary is not None and saved_id:
        primary = dict(primary)
        primary["_saved_location_id"] = saved_id
    # Only a verified result may fill the card-owned field, and only when the
    # contact had no location at all.
    if (primary and not company_location and primary.get("city")
            and not low_trust and status == "VERIFIED"):
        db.set_company_location_if_empty(
            contact_id, "%s, %s" % (primary["city"], primary.get("state") or ""))
    return primary


def _saved_location_id(primary, candidate):
    """Row id of the location this candidate wrote (not the primary row)."""
    if not primary:
        return None
    saved = primary.get("_saved_location_id")
    if saved:
        return saved
    if locations.same_location(primary, candidate):
        return primary.get("id")
    return None


def _area_point(extracted, notes, country_filter="in"):
    """Coordinates used to judge distance from the address's own area.

    A valid PIN is resolved once to a Geoapify PIN centre (cached forever in
    ``pin_areas``); the common case costs zero Geoapify requests after the
    first contact for that PIN. Fall back to the cached area/city lookup only
    when the PIN is missing, unknown, or has no coordinates.
    """
    area = pin_areas.lookup(extracted.get("postal_code"))
    if area["valid"] and area["lat"] is not None and area["lon"] is not None:
        notes.append("area from pin %s (%s, %s)" % (
            area["pin"], area.get("city") or "-", area.get("source") or "-"))
        return {"point": (area["lat"], area["lon"]), "source": "pin"}

    query = geo.area_query(extracted)
    if not query:
        return None
    key = " ".join(query.lower().split())
    cached = db.get_location_cache(key)
    if (cached and cached.get("resolved")
            and cached.get("latitude") is not None
            and cached.get("longitude") is not None):
        return {"point": (cached["latitude"], cached["longitude"]),
                "source": "city"}
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
    return {"point": (result.get("lat"), result.get("lon")), "source": "city"}


def _area_parts(reference):
    """Accept the dict shape and the old bare-tuple shape (tests, callers)."""
    if reference is None:
        return None, None
    if isinstance(reference, dict):
        return reference.get("point"), reference.get("source")
    return reference, "pin"


def _fields_for(result, extracted):
    """Keep extracted parts; fill blanks from the map without overwriting."""
    fields = project(extracted)
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
    return project(fields)


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


def _save_anchored(contact_id, website, official_site, company_location,
                   extracted, area_ref, source_type, low_trust, notes,
                   rejected, lookups):
    """Address is good but the map result is not: save it on its own anchor."""
    point, anchor_kind = _area_parts(area_ref)
    fields = _fields_for(None, extracted)
    if point:
        fields["latitude"], fields["longitude"] = point
    precision = "AREA" if anchor_kind == "pin" else "CITY"
    pin_source = ("pin_centre" if anchor_kind == "pin"
                  else "city_centre" if anchor_kind == "city" else "none")
    status = "APPROXIMATE"
    primary = _save_location(
        contact_id, fields, extracted, website, official_site,
        company_location, status=status, pin_precision=precision,
        low_trust=low_trust)
    reason = (rejected or {}).get("reason") or "no_area_anchor"
    review = ["pin_fallback", reason]
    if low_trust:
        review.append("low_company_match")
    notes.append("saved approximate/%s from %s (map: %s)"
                 % (precision, pin_source, reason))
    verdict = dict(rejected or {})
    verdict.update({
        "outcome": "approximate",
        "reason": "pin_fallback",
        "pin_precision": precision,
        "distance_km": (rejected or {}).get("distance_km"),
    })
    return {
        "primary": primary,
        "location_id": (primary or {}).get("_saved_location_id"),
        "fields": fields,
        "status": status,
        "precision": precision,
        "pin_source": pin_source,
        "rejected_map": geo.result_summary(
            (rejected or {}).get("result"), extracted),
        "review": sorted(set(review)),
        "verdict": verdict,
        "source_type": source_type,
        "extracted": extracted,
    }, "ok", lookups, verdict


def _process_address(contact_id, company, website, official_site,
                     company_location, extracted, country_filter, low_trust,
                     notes):
    """Geocode/save one extracted address.

    Returns ``(saved_or_None, reason, lookups, verdict)``.
    """
    normalized = _normalize_address(extracted)
    area_ref = _area_point(extracted, notes, country_filter)
    area, area_kind = _area_parts(area_ref)
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
                             area=area, area_kind=area_kind,
                             source_type=source_type,
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
        for kind, query in geo.lookup_queries(extracted, area=area):
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

            if kind == "landmark_city":
                raw_count = len(results)
                results = [result for result in results
                           if geo.landmark_qualified(result, extracted, area)]
                if raw_count and not results:
                    notes.append("landmark results filtered: %d outside "
                                 "name overlap or 2 km" % raw_count)
                if not results:
                    lookups.append({
                        "kind": kind, "query": query, "results": 0,
                        "best": "no_results", "reason": "landmark_not_qualified",
                        "worked": False,
                    })
                    continue

            verdicts = []
            for result in results:
                verdict = geo.assess(result, company_location, extracted,
                                     area=area, area_kind=area_kind,
                                     source_type=source_type,
                                     country_code=country_filter)
                # A landmark hit is only a locator for the area unless it is a
                # true building match; it must never claim a street pin.
                if (kind == "landmark_city"
                        and verdict["pin_precision"] != "BUILDING"):
                    verdict["pin_precision"] = "AREA"
                verdicts.append(verdict)
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
            # Stop once the candidate is verified or already street-level: the
            # remaining queries can only add coarse (CITY/AREA) duplicates and
            # each one costs a Geoapify call.
            if best and (best["outcome"] == "verified"
                         or best.get("pin_precision") in ("STREET", "BUILDING")):
                break

        if best_lookup is not None:
            lookups[best_lookup]["worked"] = True

    rejected = None
    if best is not None and best["outcome"] not in ("verified", "approximate"):
        rejected = best
        best = None
    if best is None:
        if rejected is None:
            rejected = geo.assess(None, company_location, extracted,
                                  area=area, area_kind=area_kind,
                                  source_type=source_type,
                                  country_code=country_filter)
        # Address evidence is already validated by _valid_addresses. A bad or
        # missing map result only means the pin is bad: keep the address and
        # anchor it on the PIN centre (AREA) or city centre (CITY).
        return _save_anchored(
            contact_id, website, official_site, company_location, extracted,
            area_ref, source_type, low_trust, notes, rejected, lookups)

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
        "location_id": (primary or {}).get("_saved_location_id"),
        "fields": fields,
        "status": status,
        "precision": precision,
        "pin_source": "map",
        "rejected_map": None,
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
        # rate-limit errors are never stored by address_cache.
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

        raw_addresses, extraction_error = _extract(
            company, company_location, pages)
        if extraction_error:
            # Transient/config failures are not the contact's fault and must
            # not consume an attempt or poison the company-wide failure cache.
            return finish({"ok": False, "site": "address",
                           "reason": "extraction_failed"},
                          "FAIL", "extraction_failed", mark_failed=False,
                          extra_notes=["extraction error: %s" % extraction_error])
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
                low_trust = any(page.get("company_trust") == "low"
                                for page in pages)
                raw_addresses, extraction_error = _extract(
                    company, company_location, pages)
                if extraction_error:
                    return finish({"ok": False, "site": "address",
                                   "reason": "extraction_failed"},
                                  "FAIL", "extraction_failed", mark_failed=False,
                                  extra_notes=["extraction error: %s"
                                               % extraction_error])
                valid, rejected = _valid_addresses(
                    raw_addresses, pages, country_filter, company_location)

        if not valid:
            _note_candidates(attempt, valid, rejected)
            reason = "incomplete_extraction"
            rejected_reasons = {item for _, item in rejected}
            for candidate in ("invalid_postcode", "country_mismatch",
                              "no_street_line", "incomplete",
                              "quote_not_found"):
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

        ordered, seen_addresses = [], set()
        for address in _address_order(valid, company_location):
            key = (norm(address.get("address_line1")),
                   norm(address.get("postal_code")))
            if key in seen_addresses:
                attempt["notes"].append("duplicate candidate skipped: %s"
                                        % (address.get("address_line1") or "-"))
                continue
            seen_addresses.add(key)
            ordered.append(address)
        chosen = ordered[0] if ordered else None
        _note_candidates(attempt, valid, rejected, chosen)
        notes = attempt["notes"]
        if country_filter != "in":
            notes.append("international contact: expected country %s"
                         % country_filter)

        limit = max(1, config.ADDRESS_MAX_LOCATIONS)
        batch = ordered[:limit]
        saved, reasons, lookups, review = [], [], [], []
        candidates = []
        last_verdict = None
        for index, address in enumerate(batch, start=1):
            result, reason, address_lookups, address_verdict = _process_address(
                contact_id, company, website, official_site, company_location,
                address, country_filter, low_trust, notes)
            lookups.extend(address_lookups)
            if address_verdict:
                last_verdict = address_verdict
            verdict = address_verdict or {}
            candidates.append(address_attempt.candidate(
                index, len(batch),
                extracted=address,
                address_type=address.get("address_type") or "other",
                evidence_url=address.get("evidence_url") or "",
                lookups=address_lookups,
                checks=verdict.get("checks") or [],
                map_result=geo.result_summary(verdict.get("result"), address),
                verdict=verdict.get("outcome") or
                ("saved" if result else "rejected"),
                precision=(result or {}).get("precision")
                or verdict.get("pin_precision") or "",
                distance_km=verdict.get("distance_km"),
                reason=verdict.get("reason") or reason,
                location_id=(result or {}).get("location_id"),
                accepted=bool(result),
                pin_source=(result or {}).get("pin_source") or "",
                rejected=(result or {}).get("rejected_map")))
            if result:
                saved.append(result)
                review.extend(result.get("review") or [])
                saved_key = (norm(result.get("fields", {}).get("address_line1")),
                             norm(result.get("fields", {}).get("postal_code")))
                if saved_key != (norm(address.get("address_line1")),
                                 norm(address.get("postal_code"))):
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
                          "FAIL", reason, mark_failed=True,
                          lookups=lookups, candidates=candidates)

        # merge_company_location() already re-ran the authoritative primary
        # rule after the last save, so its return value is the final row. The
        # old local re-selection could pick a different row (synthetic
        # found_count=1) and then report one row's status with another's fields.
        matched = saved[-1]
        primary = matched.get("primary") or next(
            (item.get("primary") for item in reversed(saved)
             if item.get("primary")), None)
        primary_id = (primary or {}).get("_saved_location_id") or \
            (primary or {}).get("id")
        for item in candidates:
            item["primary"] = bool(primary_id
                                   and item.get("location_id") == primary_id)
        if not any(item["primary"] for item in candidates):
            for item in candidates:
                if item.get("accepted"):
                    item["primary"] = True
                    break
        final = _status_from(primary, matched.get("status") or "APPROXIMATE")
        fields = (primary if primary and primary.get("address_line1") is not None
                  else matched.get("fields")) or {}
        if review:
            attempt["review"] = sorted(set(review))
        notes.append("primary location: %s" % ((primary or {}).get("id") or "-"))
        notes.append("saved locations: %d of %d candidates"
                     % (len(saved), len(batch)))
        address_cache.clear_failure(company, website)
        log.info("address: %s for %s (%d location(s))",
                 final, company, len(saved))
        return finish({"ok": True, "site": "address", "reason": final,
                       "address_status": final,
                       **_cache_fields(fields)},
                      "PASS", final, lookups=lookups, candidates=candidates)
    except Exception as exc:  # noqa: BLE001 - record then re-raise
        address_attempt.note(attempt, "%s: %s" % (type(exc).__name__, exc))
        address_attempt.finish(attempt, "FAIL", "unhandled_exception")
        raise
