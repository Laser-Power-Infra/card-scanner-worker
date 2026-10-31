import json
import os

import pytest

from app.subsidiary import search


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SEARCH_"):
            monkeypatch.delenv(key, raising=False)
    # Pin provider order so the tests do not depend on the developer's .env.
    monkeypatch.setattr(search.config, "SEARCH_PRIORITY", "tavily,exa,serpapi")
    search._count = 0
    search._cooldown_until.clear()


def test_search_keys_follow_explicit_provider_priority(monkeypatch):
    monkeypatch.setenv("SEARCH_SERPAPI_API_KEY", "serp")
    monkeypatch.setenv("SEARCH_EXA_API_KEY", "exa")
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY", "tavily")
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY_2", "tavily-2")
    keys = search._ordered_keys()
    assert [(provider, value) for provider, _, value in keys] == [
        ("tavily", "tavily"), ("tavily", "tavily-2"), ("exa", "exa"),
        ("serpapi", "serp"),
    ]


def test_search_retries_once_after_rate_limit(monkeypatch):
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY", "tavily")
    monkeypatch.setenv("SEARCH_EXA_API_KEY", "exa")
    calls = []

    attempts = {"tavily": 0}

    def tavily(query, key, domains, lean=False):
        calls.append("tavily")
        attempts["tavily"] += 1
        if attempts["tavily"] == 1:
            raise search.RateLimited("429")
        return [{"title": "ok", "url": "https://example.com", "text": "business"}]

    def exa(query, key, domains, lean=False):
        calls.append("exa")
        return [{"title": "ok", "url": "https://example.com", "text": "business"}]

    monkeypatch.setitem(search._ADAPTERS, "tavily", tavily)
    monkeypatch.setitem(search._ADAPTERS, "exa", exa)
    result = search.search("example company")
    assert result[0]["text"] == "business"
    assert calls == ["tavily", "tavily"]


def test_hard_rate_limit_is_not_retried_and_enters_cooldown(monkeypatch):
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY", "tavily")
    monkeypatch.setenv("SEARCH_EXA_API_KEY", "exa")
    calls = []

    def tavily(query, key, domains, lean=False):
        calls.append("tavily")
        raise search.RateLimited("432", hard=True)

    def exa(query, key, domains, lean=False):
        calls.append("exa")
        return [{"title": "ok", "url": "https://example.com", "text": "business"}]

    monkeypatch.setitem(search._ADAPTERS, "tavily", tavily)
    monkeypatch.setitem(search._ADAPTERS, "exa", exa)

    result = search.search("example company")
    assert result[0]["text"] == "business"
    # One failed tavily attempt, no retry, then exa; tavily is cooled down.
    assert calls == ["tavily", "exa"]
    assert search._cooldown_until.get("SEARCH_TAVILY_API_KEY", 0) > 0


def test_empty_results_try_other_provider_without_marking_failure(monkeypatch):
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY", "tavily")
    monkeypatch.setenv("SEARCH_EXA_API_KEY", "exa")
    calls = []

    def empty(query, key, domains, lean=False):
        calls.append(key)
        return [] if key == "tavily" else [{"title": "found", "url": "https://example.com", "text": "text"}]

    monkeypatch.setitem(search._ADAPTERS, "tavily", empty)
    monkeypatch.setitem(search._ADAPTERS, "exa", empty)
    assert search.search("example company")[0]["title"] == "found"
    assert calls == ["tavily", "exa"]


def test_key_suffix_sort_is_numeric(monkeypatch):
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY_10", "key-10")
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY_2", "key-2")
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY", "key-1")
    keys = search._ordered_keys()
    assert [key for _, _, key in keys] == ["key-1", "key-2", "key-10"]


def test_no_configured_search_key_pauses(monkeypatch):
    with pytest.raises(search.SearchExhausted):
        search.search("example company")


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise search.requests.HTTPError("status %s" % self.status_code)


def test_firecrawl_scrapes_markdown_and_sends_domain_filter(monkeypatch):
    calls = []
    payload = {"success": True, "data": {"web": [
        {"url": "https://a.example", "title": "A",
         "markdown": "# A\nbusiness", "description": "short"},
        {"url": "https://b.example", "title": "B", "description": "fallback"},
        {"title": "no url"},
    ]}}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "body": json, "headers": headers,
                      "timeout": timeout})
        return _FakeResponse(payload)

    monkeypatch.setattr(search.requests, "post", fake_post)
    results = search._firecrawl("query", "fc-key", ["example.com"])

    assert [item["url"] for item in results] == ["https://a.example",
                                                 "https://b.example"]
    assert results[0]["text"] == "# A\nbusiness"
    assert results[1]["text"] == "fallback"
    call = calls[0]
    assert call["url"] == "https://api.firecrawl.dev/v2/search"
    assert call["headers"]["Authorization"] == "Bearer fc-key"
    assert call["body"]["query"] == "query"
    assert call["body"]["limit"] == search.config.SEARCH_MAX_RESULTS
    assert call["body"]["sources"] == [{"type": "web"}]
    assert call["body"]["includeDomains"] == ["example.com"]
    assert call["body"]["scrapeOptions"] == {
        "formats": ["markdown"], "onlyMainContent": True,
        "timeout": search.config.SEARCH_SCRAPE_TIMEOUT * 1000}
    assert call["timeout"] == search.config.SEARCH_SCRAPE_TIMEOUT + 20


def test_firecrawl_lean_skips_scrape_options(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return _FakeResponse({"success": True, "data": {"web": [
            {"url": "https://a.example", "title": "A",
             "description": "desc"}]}})

    monkeypatch.setattr(search.requests, "post", fake_post)
    results = search._firecrawl("query", "fc-key", [], lean=True)
    assert "scrapeOptions" not in calls[0]
    assert results[0]["text"] == "desc"


def test_firecrawl_omits_domain_filter_without_domains(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return _FakeResponse({"success": True, "data": {"web": []}})

    monkeypatch.setattr(search.requests, "post", fake_post)
    assert search._firecrawl("query", "fc-key", []) == []
    assert "includeDomains" not in calls[0]


def test_firecrawl_accepts_v1_list_payload(monkeypatch):
    payload = {"success": True, "data": [
        {"url": "https://a.example", "title": "A", "description": "desc"}]}
    monkeypatch.setattr(search.requests, "post",
                        lambda *args, **kwargs: _FakeResponse(payload))
    assert search._firecrawl("query", "fc-key", []) == [
        {"title": "A", "url": "https://a.example", "text": "desc"}]


def test_firecrawl_credit_failure_is_hard_rate_limit(monkeypatch):
    payload = {"success": False, "error": "Insufficient credits remaining"}
    monkeypatch.setattr(search.requests, "post",
                        lambda *args, **kwargs: _FakeResponse(payload))
    with pytest.raises(search.RateLimited) as excinfo:
        search._firecrawl("query", "fc-key", [])
    assert excinfo.value.hard


def test_firecrawl_other_failure_is_transient(monkeypatch):
    payload = {"success": False, "error": "unable to process request"}
    monkeypatch.setattr(search.requests, "post",
                        lambda *args, **kwargs: _FakeResponse(payload))
    with pytest.raises(search.TransientError):
        search._firecrawl("query", "fc-key", [])


def test_firecrawl_key_discovered_in_priority_order(monkeypatch):
    monkeypatch.setenv("SEARCH_EXA_API_KEY", "exa")
    monkeypatch.setenv("SEARCH_FIRECRAWL_API_KEY", "fc")
    monkeypatch.setattr(search.config, "SEARCH_PRIORITY", "exa,firecrawl")
    keys = search._ordered_keys()
    assert [(provider, value) for provider, _, value in keys] == [
        ("exa", "exa"), ("firecrawl", "fc")]


def test_empty_provider_falls_through_to_firecrawl(monkeypatch):
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY", "tavily")
    monkeypatch.setenv("SEARCH_FIRECRAWL_API_KEY", "fc")
    monkeypatch.setattr(search.config, "SEARCH_PRIORITY", "tavily,firecrawl")
    calls = []

    def tavily(query, key, domains, lean=False):
        calls.append("tavily")
        return []

    def firecrawl(query, key, domains, lean=False):
        calls.append("firecrawl")
        return [{"title": "ok", "url": "https://example.com", "text": "body"}]

    monkeypatch.setitem(search._ADAPTERS, "tavily", tavily)
    monkeypatch.setitem(search._ADAPTERS, "firecrawl", firecrawl)
    result = search.search("example company")
    assert result[0]["url"] == "https://example.com"
    assert calls == ["tavily", "firecrawl"]


def test_serpapi_out_of_searches_is_hard_rate_limit(monkeypatch):
    payload = {"error": "Your account has run out of searches."}
    monkeypatch.setattr(search.requests, "get",
                        lambda *args, **kwargs: _FakeResponse(payload))
    with pytest.raises(search.RateLimited) as excinfo:
        search._serpapi("query", "serp-key", [])
    assert excinfo.value.hard


def test_serpapi_out_of_searches_is_not_retried(monkeypatch):
    monkeypatch.setenv("SEARCH_SERPAPI_API_KEY", "serp")
    monkeypatch.setattr(search.config, "SEARCH_PRIORITY", "serpapi")
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(1)
        return _FakeResponse({"error": "Your account has run out of searches."})

    monkeypatch.setattr(search.requests, "get", fake_get)
    with pytest.raises(search.SearchExhausted):
        search.search("example company")
    assert len(calls) == 1
    assert search._cooldown_until["SEARCH_SERPAPI_API_KEY"] > 0


def test_exhausted_quota_message_is_hard_rate_limit(monkeypatch):
    payload = {"success": False, "error": "Search credits exhausted for this key"}
    monkeypatch.setattr(search.requests, "post",
                        lambda *args, **kwargs: _FakeResponse(payload))
    with pytest.raises(search.RateLimited) as excinfo:
        search._firecrawl("query", "fc-key", [])
    assert excinfo.value.hard
