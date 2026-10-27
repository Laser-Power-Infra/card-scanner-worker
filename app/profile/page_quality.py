"""Page text, link discovery, and retrieval ranking for the address pass.

Pure helpers: no requests, no database. The address worker does the fetching;
this module decides what a page says and whether it is worth sending to the
extraction model.
"""
import math
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from . import india_geo
from .address_text import compact as _compact
from .address_text import norm as _normalize_quote

ADDRESS_HINT = re.compile(
    r"\b(address|registered office|corporate office|head office|"
    r"headquarter|h\.?q\.?)\b", re.I)
_OFFICE_HINT = re.compile(
    r"\b(registered office|corporate office|head office|headquarter|"
    r"works|factory|branch office)\b", re.I)
# Indian PINs are often printed as "700 160" or "700-160" on contact pages.
_PIN = re.compile(r"\b[1-9]\d{2}[\s-]?\d{3}\b")
_STREET = re.compile(
    r"\b(street|road|lane|nagar|colony|sector|block|building|floor|plot|"
    r"avenue|marg|sarani|park|p\.?o\.?|p\.?s\.?)\b", re.I)
_PAGE_LINK = re.compile(
    r"(contact|about|location|reach|office|branch|network|enquir|address|"
    r"get-in-touch)", re.I)
_DEAD = re.compile(
    r"(account suspended|website (?:is )?suspended|site (?:is )?suspended|"
    r"domain (?:is )?for sale|domain (?:has )?expired|parked domain|"
    r"site (?:is )?under construction|404 not found|page not found|"
    r"this site can.t be reached)", re.I)
_JS_JUNK = re.compile(
    r"(document\.documentElement|gtm\.start|jquery|window\.|"
    r"function\s*\(|addEventListener)", re.I)
_COMMENT = re.compile(r"(?is)<!--.*?-->")
_SCRIPT = re.compile(r"(?is)<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>")
_TAG = re.compile(r"(?is)<[^>]+>")
_LOC = re.compile(r"(?is)<loc>\s*(.*?)\s*</loc>")

_HEAD_CHARS = 8000
_TAIL_CHARS = 4000
_MIN_PAGE_CHARS = 120

_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")
_GENERIC_NAME_WORDS = {
    "pvt", "private", "ltd", "limited", "llp", "inc", "incorporated", "india",
    "indian", "the", "and", "of", "co", "company", "corporation", "corp",
    "opc", "plc", "govt", "government", "department", "directorate",
}


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


def html_to_text(html):
    """Strip scripts/styles before tags so page text is not CSS/JS noise."""
    text = _SCRIPT.sub(" ", html or "")
    text = _COMMENT.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = unescape(text).replace("\xa0", " ")
    return " ".join(text.split())


def condense(text, head=_HEAD_CHARS, tail=_TAIL_CHARS):
    """Keep the start and end of a page instead of only its first N chars."""
    text = text or ""
    if len(text) <= head + tail:
        return text
    return text[:head] + " [...] " + text[-tail:]


def url_key(url):
    """Dedupe key: ignore www, trailing slash, case of host, and fragments."""
    parsed = urlparse(str(url or "").strip())
    if not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "/").rstrip("/") or "/"
    return "%s%s%s" % (host, path, ("?" + parsed.query) if parsed.query else "")


def _host(url):
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def extract_links(html, base_url):
    """Same-site links worth following, contact-like first."""
    parser = _LinkParser()
    try:
        parser.feed(html or "")
    except Exception:  # noqa: BLE001 - malformed HTML is common
        pass
    base_host = _host(base_url)
    ranked, seen = [], set()
    for href, anchor in parser.links:
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        host = _host(url)
        if not host or not base_host:
            continue
        if not (host == base_host or host.endswith("." + base_host)):
            continue
        haystack = "%s %s" % (parsed.path, anchor)
        if not _PAGE_LINK.search(haystack):
            continue
        key = url_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        priority = 0 if re.search(r"contact|reach|address|get-in-touch", haystack,
                                  re.I) else 1
        ranked.append((priority, url))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [url for _, url in ranked]


def is_page_candidate_url(url):
    return bool(_PAGE_LINK.search(urlparse(str(url or "")).path))


def parse_sitemap(xml):
    """All <loc> URLs in a sitemap or sitemap index."""
    return [unescape(match).strip() for match in _LOC.findall(xml or "")]


def has_address_signal(text):
    """True when an address hint sits next to a pin or street line.

    A bare 'Head Office' nav label must not suppress the search fallback.
    """
    text = text or ""
    for match in ADDRESS_HINT.finditer(text):
        window = text[max(0, match.start() - 120):match.start() + 320]
        if _PIN.search(window) or _STREET.search(window):
            return True
    return False


def has_street_address(text):
    """Stricter than has_address_signal: a street/building token *and* a locator.

    Used to decide whether a site page is worth sending to extraction without
    a wider search. "Head Office, Hyderabad 500081" is not enough; the page
    needs a street/plot/floor/building word too.
    """
    text = text or ""
    locator = bool(_STREET.search(text) or re.search(
        r"\b(plot|building|floor|flat|tower|house|block|unit)\b", text, re.I))
    if not locator:
        return False
    return bool(_PIN.search(text))


def is_dead_page(page):
    title = (page or {}).get("title") or ""
    text = (page or {}).get("text") or ""
    return bool(_DEAD.search(title) or _DEAD.search(text[:500]))


def score_page(page, company="", company_location=""):
    """Rank a page by how likely it is to contain the company's address."""
    page = page or {}
    text = page.get("text") or ""
    title = page.get("title") or ""
    url = page.get("url") or ""
    combined = "%s %s" % (title, text)
    score = 0

    if ADDRESS_HINT.search(combined):
        score += 4
    if _PIN.search(combined):
        score += 4
    if _OFFICE_HINT.search(combined):
        score += 3
    if has_address_signal(text):
        score += 3
    if _PAGE_LINK.search(urlparse(url).path or ""):
        score += 1

    company_tokens = [token for token in re.findall(r"[a-z0-9]+",
                                                    (company or "").lower())
                      if len(token) >= 4]
    if company_tokens and any(token in combined.lower()
                              for token in company_tokens):
        score += 2

    location_tokens = [token for token in re.findall(r"[a-z]+",
                                                      (company_location or "").lower())
                       if len(token) >= 4]
    if location_tokens and any(token in combined.lower()
                               for token in location_tokens):
        score += 1

    if _DEAD.search(title) or _DEAD.search(text[:500]):
        score -= 30
    if len(_JS_JUNK.findall(text)) >= 3 and not _PIN.search(text):
        score -= 8
    if len(text) < 200:
        score -= 3
    return score


def select_pages(pages, company="", company_location="", limit=5):
    """Drop dead/thin pages, dedupe, rank by score, keep the best ``limit``."""
    kept, seen = [], set()
    for page in pages or ():
        page = page or {}
        text = page.get("text") or ""
        key = url_key(page.get("url")) or text[:80]
        if not text or len(text) < _MIN_PAGE_CHARS or key in seen:
            continue
        if is_dead_page(page):
            continue
        seen.add(key)
        kept.append(page)
    kept.sort(key=lambda page: (-score_page(page, company, company_location),
                                page.get("url") or ""))
    return kept[:limit]


def company_tokens(company, company_location=""):
    """Distinctive name words after dropping legal suffixes, 'India', and
    state/city words supplied by the card. Location words belong to the
    location check, not the name check."""
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    location_words = set(re.findall(r"[a-z0-9]+",
                                    (company_location or "").lower()))
    from . import address_fields, pin_codes
    generic = set(_GENERIC_NAME_WORDS)
    for state in pin_codes.states():
        generic.update(re.findall(r"[a-z0-9]+", state.lower()))
    generic.update(address_fields.city_state_map())
    return [word for word in words
            if len(word) >= 3 and word not in generic
            and word not in location_words]


def _location_match(text, company_location):
    """True when the page names the contact's city/state or its GSTIN state.

    The old version only accepted the exact state string, which dropped nearly
    every page for a contact whose card said "Telangana" while the page said
    "Hyderabad". City→state aliases close that gap.
    """
    location = " ".join(str(company_location or "").lower().split())
    if not location:
        return True
    if location in text:
        return True
    if any(len(token) >= 4 and token in text for token in location.split()):
        return True

    from . import address_fields
    city_state = address_fields.city_state_map()
    for city, state in city_state.items():
        if city in text and (city in location or state in location):
            return True
        if city in location and state in text:
            return True

    prefixes = {gstin[:2] for gstin in _GSTIN.findall(text.upper())}
    if not prefixes:
        return False
    expected = india_geo.gst_prefix_for(location)
    return bool(expected and expected in prefixes)


def company_match(page, company, company_location="", website=""):
    """Return (tier, reasons) for a candidate page.

    tier is one of high / medium / low / none.
      high   compact core phrase, exact name phrase, or matching domain;
      medium at least half the core words appear;
      low    only the city/state/GSTIN matches;
      none   nothing matches.

    The retrieval worker keeps high/medium. If none exist it keeps the
    best-scored low pages and marks the result low-trust instead of failing.
    """
    page = page or {}
    text = " ".join(((page.get("title") or ""), (page.get("text") or ""))).lower()
    if not text.strip():
        return "none", ["empty"]

    tokens = company_tokens(company, company_location)
    phrase = " ".join(tokens)
    compact_phrase = _compact(phrase)
    compact_text = _compact(text)
    reasons = []

    if compact_phrase and len(compact_phrase) >= 4 and compact_phrase in compact_text:
        return "high", ["compact_name"]

    if phrase and re.search(r"\b%s\b" % re.escape(phrase), text):
        return "high", ["exact_name"]

    # Domain match uses the page's own host, not the contact's website field:
    # a directory page about another company must not become high-trust just
    # because we know the contact's domain.
    # Match a host label, never a substring of one: "air" must not make
    # fairdealdirectory.in look like Air India. Require at least four
    # characters so short generic words cannot match.
    host = _compact(urlparse(str(page.get("url") or "")).hostname or "")
    labels = {_compact(part) for part in
              (urlparse(str(page.get("url") or "")).hostname or "").lower().split(".")
              if part}
    if host and compact_phrase and (
            compact_phrase in labels
            or any(len(_compact(token)) >= 4
                   and (_compact(token) in labels
                        or any(_compact(token) in label for label in labels))
                   for token in tokens)):
        return "high", ["domain"]

    hits = [token for token in tokens
            if re.search(r"\b%s\b" % re.escape(token), text)]
    if tokens and len(hits) == len(tokens):
        return "high", ["all_name_words"]
    required = max(1, math.ceil(len(tokens) * 0.5)) if tokens else 0
    if tokens and len(hits) >= required:
        reasons.append("name_words=%d/%d" % (len(hits), len(tokens)))
        return "medium", reasons

    if _location_match(text, company_location):
        reasons.append("location_only")
        return "low", reasons
    return "none", ["no_name_or_location"]


def quote_in_page(quote, page_text):
    """True when the model's quote is a contiguous span of the page text."""
    needle = _normalize_quote(quote)
    haystack = _normalize_quote(page_text)
    return len(needle) >= 12 and needle in haystack
