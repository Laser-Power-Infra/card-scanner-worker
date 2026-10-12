import os

import pytest

from app.subsidiary import search


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SEARCH_"):
            monkeypatch.delenv(key, raising=False)
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

    def tavily(query, key, domains):
        calls.append("tavily")
        attempts["tavily"] += 1
        if attempts["tavily"] == 1:
            raise search.RateLimited("429")
        return [{"title": "ok", "url": "https://example.com", "text": "business"}]

    def exa(query, key, domains):
        calls.append("exa")
        return [{"title": "ok", "url": "https://example.com", "text": "business"}]

    monkeypatch.setitem(search._ADAPTERS, "tavily", tavily)
    monkeypatch.setitem(search._ADAPTERS, "exa", exa)
    result = search.search("example company")
    assert result[0]["text"] == "business"
    assert calls == ["tavily", "tavily"]


def test_empty_results_try_other_provider_without_marking_failure(monkeypatch):
    monkeypatch.setenv("SEARCH_TAVILY_API_KEY", "tavily")
    monkeypatch.setenv("SEARCH_EXA_API_KEY", "exa")
    calls = []

    def empty(query, key, domains):
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
