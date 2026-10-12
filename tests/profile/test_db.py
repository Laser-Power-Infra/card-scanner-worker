"""Tests for the profile worker's raw-SQL field filtering (no live DB)."""
from app.profile import db


def test_merge_enrichment_filters_to_writable_non_empty_columns(monkeypatch):
    captured = {}
    monkeypatch.setattr(db, "upsert_enrichment", lambda cid: None)

    def fake_run(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params

    monkeypatch.setattr(db, "_run", fake_run)

    db.merge_enrichment("c1", {
        "summary": "Hello",
        "location": "   ",        # blank -> dropped
        "not_a_column": "x",      # not writable -> dropped
        "official_site": None,    # None -> dropped
    })

    assert '"summary" = %s' in captured["sql"]
    assert "not_a_column" not in captured["sql"]
    assert "official_site" not in captured["sql"]
    assert captured["params"] == ["Hello", "c1"]


def test_merge_enrichment_noop_when_nothing_writable(monkeypatch):
    monkeypatch.setattr(db, "upsert_enrichment", lambda cid: None)
    monkeypatch.setattr(db, "_run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no write")))
    db.merge_enrichment("c1", {"summary": "", "not_a_column": "x"})


def test_status_enum_maps_lowercase_to_prisma_values():
    assert db._STATUS_ENUM["running"] == "RUNNING"
    assert db._STATUS_ENUM["partial"] == "PARTIAL"
    assert set(db._STATUS_ENUM) == {"pending", "running", "done", "partial", "failed"}
