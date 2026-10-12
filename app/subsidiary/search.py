"""External search adapters with SEARCH_* key discovery and graceful rotation."""
from collections import defaultdict
import logging
import os
import re
import time
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger("subsidiary.search")


class RateLimited(Exception):
    def __init__(self, message="rate limited", hard=False):
        super().__init__(message)
        self.hard = hard


class TransientError(Exception):
    pass


class SearchExhausted(Exception):
    """No configured search key can serve a request right now."""


class NoSearchResults(Exception):
    """All providers succeeded but returned no usable results."""


_count = 0
_cooldown_until = {}
_CREDIT_RE = re.compile(r"credit|quota|billing|limit reached|insufficient", re.I)
_GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "icloud.com", "protonmail.com",
    "proton.me", "rediffmail.com", "gmx.com", "yopmail.com",
}


def _generic_domains():
    """Built-in generic domains plus any comma-separated extras from env."""
    extras = {d.strip().lower() for d in config.GENERIC_EMAIL_DOMAINS.split(",")
              if d.strip()}
    return _GENERIC_EMAIL_DOMAINS | extras


def _search_vars():
    """Return configured provider keys, accepting numbered suffixes too."""
    pattern = re.compile(r"^SEARCH_([A-Z0-9]+)_API_KEY(?:_([0-9]+))?$")
    found = []
    for name, value in os.environ.items():
        match = pattern.match(name)
        if match and value.strip():
            suffix = int(match.group(2) or "1")
            found.append((match.group(1).lower(), suffix, name, value.strip()))
    return found


def _ordered_keys():
    priority = [p.strip().lower() for p in config.SEARCH_PRIORITY.split(",") if p.strip()]
    groups = defaultdict(list)
    for provider, suffix, name, value in _search_vars():
        groups[provider].append((suffix, name, value))
    provider_order = priority + [p for p in sorted(groups) if p not in priority]
    return [(provider, name, value)
            for provider in provider_order
            for _, name, value in sorted(groups.get(provider, []))]


def _response(response, provider):
    if response.status_code == 429:
        text = response.text[:300]
        raise RateLimited(text, hard=bool(_CREDIT_RE.search(text)))
    if response.status_code == 401:
        raise TransientError("search authentication error: %s" % response.text[:300])
    if response.status_code in (402, 403):
        text = response.text[:300]
        if response.status_code == 403 and not _CREDIT_RE.search(text):
            raise TransientError("search permission error: %s" % text)
        raise RateLimited(text, hard=True)
    if response.status_code >= 500:
        raise TransientError("search provider returned %s" % response.status_code)
    response.raise_for_status()
    return response.json()


def _tavily(query, key, domains):
    body = {
        "api_key": key,
        "query": query,
        "search_depth": "advanced",
        "include_raw_content": True,
        "max_results": config.SEARCH_MAX_RESULTS,
    }
    if domains:
        body["include_domains"] = domains
    response = requests.post("https://api.tavily.com/search", json=body,
                             headers={"Authorization": "Bearer " + key},
                             timeout=config.SEARCH_SCRAPE_TIMEOUT + 20)
    data = _response(response, "tavily")
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "text": r.get("raw_content") or r.get("content") or ""}
            for r in data.get("results", []) if r.get("url")]


def _exa(query, key, domains):
    body = {"query": query, "numResults": config.SEARCH_MAX_RESULTS,
            "contents": {"text": {"maxCharacters": 12000}}}
    if domains:
        body["includeDomains"] = domains
    response = requests.post("https://api.exa.ai/search", json=body,
                             headers={"x-api-key": key},
                             timeout=config.SEARCH_SCRAPE_TIMEOUT + 20)
    data = _response(response, "exa")
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "text": r.get("text", "") if isinstance(r.get("text"), str) else ""}
            for r in data.get("results", []) if r.get("url")]


def _serpapi(query, key, _domains):
    response = requests.get("https://serpapi.com/search.json",
                            params={"engine": "google", "q": query,
                                    "num": config.SEARCH_MAX_RESULTS, "api_key": key},
                            timeout=config.SEARCH_SCRAPE_TIMEOUT + 20)
    data = _response(response, "serpapi")
    api_error = str(data.get("error", ""))
    if api_error:
        if _CREDIT_RE.search(api_error):
            raise RateLimited(api_error, hard=True)
        if "rate" in api_error.lower() or "too many" in api_error.lower():
            raise RateLimited(api_error)
        raise TransientError(api_error)
    results = []
    for item in data.get("organic_results", []):
        url = item.get("link", "")
        if not url:
            continue
        text = item.get("snippet", "")
        try:
            page = requests.get(url, timeout=config.SEARCH_SCRAPE_TIMEOUT,
                                headers={"User-Agent": "CardScanner subsidiary research"})
            if page.ok and "text/html" in page.headers.get("content-type", ""):
                from html import unescape
                text += " " + unescape(re.sub(r"<[^>]+>", " ", page.text))[:12000]
        except requests.RequestException:
            log.debug("Could not fetch search result page %s", url)
        results.append({"title": item.get("title", ""), "url": url, "text": text})
    return results


_ADAPTERS = {"tavily": _tavily, "exa": _exa, "serpapi": _serpapi}


def _direct_domain(domains):
    for domain in domains:
        raw = domain.lower().strip()
        host = urlparse(raw if "://" in raw else "//" + raw).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        if host and host not in _generic_domains() and "." in host:
            return host
    return ""


def _scrape_domain(domain):
    for scheme in ("https://", "http://"):
        url = scheme + domain
        try:
            response = requests.get(url, timeout=config.SEARCH_SCRAPE_TIMEOUT,
                                    headers={"User-Agent": "CardScanner subsidiary research"})
            if response.status_code == 429:
                raise RateLimited("Corporate website rate-limited", hard=False)
            if response.status_code in (402, 403) and _CREDIT_RE.search(response.text[:300]):
                raise RateLimited("Corporate website credits/quota exhausted", hard=True)
            if response.ok and "text/html" in response.headers.get("content-type", ""):
                from html import unescape
                text = unescape(re.sub(r"<[^>]+>", " ", response.text))
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return [{"title": domain, "url": response.url, "text": text[:30000]}]
        except RateLimited:
            raise
        except requests.RequestException as exc:
            _cooldown_until["direct:" + domain] = time.time() + config.DIRECT_SITE_FAILURE_COOLDOWN
            log.info("Direct website fetch failed for %s: %s", domain, exc)
            raise TransientError(str(exc)) from exc
    return []


def search(query, domains=None):
    """Try direct corporate site, then sticky priority with key-level cooldown."""
    global _count
    domains = [d for d in (domains or []) if d]
    domain = _direct_domain(domains)
    if domain and _cooldown_until.get("direct:" + domain, 0) <= time.time():
        try:
            direct = _scrape_domain(domain)
        except RateLimited as exc:
            log.warning("Corporate site %s rate-limited; falling back to search APIs", domain)
            _cooldown_until["direct:" + domain] = time.time() + (
                config.SEARCH_CREDIT_COOLDOWN if exc.hard else config.SEARCH_SOFT_COOLDOWN)
            direct = []
        except (requests.RequestException, ValueError) as exc:
            _cooldown_until["direct:" + domain] = time.time() + config.DIRECT_SITE_FAILURE_COOLDOWN
            log.info("Direct website failed; falling back to search APIs (%s)", exc)
            direct = []
        if direct:
            return direct

    keys = _ordered_keys()
    if not keys:
        raise SearchExhausted("No SEARCH_*_API_KEY variables are configured")

    count = len(keys)
    last_error = None
    for _ in range(count):
        index = _count % count
        provider, env_name, key = keys[index]
        if _cooldown_until.get(env_name, 0) > time.time():
            _count = (_count + 1) % count
            continue
        adapter = _ADAPTERS.get(provider)
        if not adapter:
            log.warning("No adapter for configured search provider %s", provider)
            _cooldown_until[env_name] = time.time() + config.SEARCH_CREDIT_COOLDOWN
            _count = (_count + 1) % count
            continue
        had_rate_limit = False
        empty_result = False
        for _provider_attempt in range(2):
            try:
                results = adapter(query, key, [domain] if domain else [])
                if results:
                    return results
                log.info("Search provider %s returned no results", provider)
                raise NoSearchResults("Provider returned no results")
            except NoSearchResults:
                # No hit from one provider is still worth checking against the
                # remaining providers in configured fallback order.
                empty_result = True
                break
            except RateLimited as exc:
                if not had_rate_limit:
                    had_rate_limit = True
                    log.info("Search key %s rate-limited; retrying once", env_name)
                    continue
                last_error = exc
                cooldown = (config.SEARCH_CREDIT_COOLDOWN if exc.hard
                            else config.SEARCH_SOFT_COOLDOWN)
                _cooldown_until[env_name] = time.time() + cooldown
                log.warning("Search key %s rate-limited; cooldown=%ss", env_name, cooldown)
                _count = (_count + 1) % count
                break
            except (TransientError, requests.RequestException, ValueError) as exc:
                last_error = exc
                log.warning("Search provider %s failed transiently: %s", provider, exc)
                _count = (_count + 1) % count
                break
        if empty_result:
            _count = (_count + 1) % count

    now = time.time()
    eligible_keys = [(provider, var) for provider, var, _ in keys if provider in _ADAPTERS]
    if not eligible_keys or all(_cooldown_until.get(var, 0) > now
                                for _, var in eligible_keys):
        raise SearchExhausted("All configured search keys are cooling down or exhausted")
    if last_error:
        raise NoSearchResults(str(last_error))
    raise NoSearchResults("All available providers returned no usable results")


def reset_rotation():
    global _count
    _count = 0


def provider_available():
    """Return whether any configured key is outside cooldown, without spending a request."""
    keys = _ordered_keys()
    if not keys:
        return False
    now = time.time()
    eligible = [(provider, name, key) for provider, name, key in keys
                if _cooldown_until.get(name, 0) <= now and provider in _ADAPTERS]
    if not eligible:
        return False
    global _count
    _count = 0
    return True
