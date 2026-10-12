"""End-to-end handler tests with fake db/search/ai boundaries.

These exercise the full process_company flow without a database, a network
call, or an LLM. They are the regression net for the handler wiring itself:
a fresh company must search -> extract -> score -> write tags, and the
aggregate-only path must never touch search.
"""
import pytest

from app.subsidiary import config, handlers, logic


def _subsidiaries():
    return [{
        "subsidiary": "LASER",
        "threshold": 0.5,
        "phrases": ["lt cable"],
        "keywords": [{
            "phrase": "lt cable", "category": "CABLES", "role": "OWN_BUSINESS",
            "aliasGroup": None, "negativeStrength": None,
        }],
    }]


class FakeDB:
    def __init__(self, header=None):
        self.header_row = header
        self.matches = None
        self.calls = []

    def get_subsidiaries(self):
        return _subsidiaries()

    def header(self, company):
        return self.header_row

    def company_matches(self, company):
        return self.matches

    def clear_matches(self, company):
        self.calls.append(("clear_matches", company))
        self.matches = []

    def get_company_sources(self, company):
        return {}

    def persist_search_results(self, company, extract_version, sources):
        self.calls.append(("persist_search_results", company))

    def upsert_header(self, company, status, extract_version, source_data):
        self.header_row = {"company": company, "status": status,
                           "extractVersion": extract_version, "sources": source_data}

    def get_keyword_vector_cache(self, phrase):
        return None

    def write_vectors(self, company, phrases, vectors, extract_version, score_version_map):
        self.calls.append(("write_vectors", list(phrases)))

    def write_matches(self, company, matches):
        self.calls.append(("write_matches", list(matches)))
        self.matches = list(matches)


def _score_version_map():
    return logic.score_versions(config.LLM_MODEL, handlers.SCORE_PROMPT_VERSION,
                                _subsidiaries())


def test_process_company_writes_matches_for_fresh_company(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(handlers, "db", fake)
    monkeypatch.setattr(handlers.ai, "extract_phrases",
                        lambda company, state, sources, subsidiaries: ["lt cable"])
    monkeypatch.setattr(handlers.ai, "score_phrases",
                        lambda phrases, subsidiaries: {"lt cable": {"LASER": 1.0}})
    monkeypatch.setattr(handlers.search, "search",
                        lambda query, domains=None: [{"url": "https://x.example",
                                                      "text": "lt cable manufacturer"}])

    outcome, result = handlers.process_company({"company": "ACME Cables", "hint": {}})

    assert outcome == "ok"
    assert result["status"] == "OK"
    assert result["matches"] == ["LASER"]
    assert ("write_matches", ["LASER"]) in fake.calls


def test_process_company_no_data_clears_matches_without_writing(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(handlers, "db", fake)
    monkeypatch.setattr(handlers.search, "search", lambda query, domains=None: [])

    outcome, result = handlers.process_company({"company": "Empty Co", "hint": {}})

    assert outcome == "ok"
    assert result["status"] == "NO_DATA"
    assert ("clear_matches", "Empty Co") in fake.calls
    assert not any(call[0] == "write_matches" for call in fake.calls)


def test_aggregate_only_never_searches(monkeypatch):
    versions = _score_version_map()
    header = {
        "company": "ACME Cables", "status": "OK", "extractVersion": "any",
        "sources": {
            "phrases": ["lt cable"],
            "keywordVectors": {
                "lt cable": {"scores": {"LASER": 1.0},
                             "scoreVersions": {"LASER": versions["LASER"]}},
            },
        },
    }
    fake = FakeDB(header=header)
    monkeypatch.setattr(handlers, "db", fake)

    def boom(*args, **kwargs):
        raise AssertionError("aggregate_only must not call search")

    monkeypatch.setattr(handlers.search, "search", boom)

    outcome, result = handlers.process_company(
        {"company": "ACME Cables", "aggregate_only": True, "hint": {}})

    assert outcome == "ok"
    assert result["matches"] == ["LASER"]
    assert ("write_matches", ["LASER"]) in fake.calls


def test_process_payload_wraps_unexpected_errors_as_error(monkeypatch):
    monkeypatch.setattr(handlers, "db", FakeDB())

    def broken(company, state, sources, subsidiaries):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr(handlers.search, "search",
                        lambda query, domains=None: [{"url": "https://x", "text": "text"}])
    monkeypatch.setattr(handlers.ai, "extract_phrases", broken)

    outcome, value = handlers.process_payload({"company": "ACME Cables"})

    assert outcome == "error"
    assert "extractor exploded" in value
