"""Unit tests for the raw-SQL helpers that do not need a live database."""
from app.subsidiary import db


def test_vector_cache_merges_per_subsidiary_newest_first(monkeypatch):
    monkeypatch.setattr(db, "get_subsidiary_names", lambda: ["LASER", "GMD"])
    captured = {}

    def fetchall(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [
            {"scores": {"LASER": 1.0}, "scoreVersions": {"LASER": "v1"}},
            {"scores": {"LASER": 0.33, "GMD": 0.66},
             "scoreVersions": {"LASER": "v0", "GMD": "v1"}},
        ]

    monkeypatch.setattr(db, "_fetchall", fetchall)

    out = db.get_keyword_vector_cache("lt cable")

    assert out["scores"] == {"LASER": 1.0, "GMD": 0.66}
    assert out["scoreVersions"] == {"LASER": "v1", "GMD": "v1"}
    # The lookup is indexed on the phrase column, not a full header scan.
    assert captured["params"] == ("lt cable",)
    assert '"keyword" = %s' in captured["sql"]


def test_vector_cache_parses_json_strings_and_drops_unknown_subsidiaries(monkeypatch):
    monkeypatch.setattr(db, "get_subsidiary_names", lambda: ["LASER"])
    monkeypatch.setattr(db, "_fetchall", lambda sql, params=None: [
        {"scores": '{"LASER": 0.66, "UNKNOWN": 1.0}',
         "scoreVersions": '{"LASER": "v1", "UNKNOWN": "v1"}'},
    ])

    out = db.get_keyword_vector_cache("gate valve")

    assert out["scores"] == {"LASER": 0.66}
    assert "UNKNOWN" not in out["scoreVersions"]


def test_vector_cache_returns_none_when_empty(monkeypatch):
    monkeypatch.setattr(db, "get_subsidiary_names", lambda: ["LASER"])
    monkeypatch.setattr(db, "_fetchall", lambda sql, params=None: [])
    assert db.get_keyword_vector_cache("nothing") is None
    assert db.get_keyword_vector_cache("") is None


def test_has_extraction_requires_ok_header_with_phrases(monkeypatch):
    monkeypatch.setattr(db, "header", lambda company: {
        "status": "OK",
        "sources": '{"phrases": ["lt cable"]}',
    })
    assert db.has_extraction("ACME Cables") is True

    monkeypatch.setattr(db, "header", lambda company: {
        "status": "OK", "sources": {"phrases": []},
    })
    assert db.has_extraction("ACME Cables") is False

    monkeypatch.setattr(db, "header", lambda company: {
        "status": "NO_DATA", "sources": {"phrases": ["lt cable"]},
    })
    assert db.has_extraction("ACME Cables") is False

    monkeypatch.setattr(db, "header", lambda company: None)
    assert db.has_extraction("ACME Cables") is False
