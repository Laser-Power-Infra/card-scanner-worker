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


def _candidate(**overrides):
    base = {
        "address_line1": "12 Park Street",
        "address_line2": "",
        "city": "Kolkata",
        "state": "West Bengal",
        "postal_code": "700022",
        "country": "India",
        "latitude": 22.5,
        "longitude": 88.3,
        "source_type": "OWN_SITE",
        "precision": "STREET",
        "status": "VERIFIED",
        "evidence_saved": True,
        "found_count": 1,
    }
    base.update(overrides)
    return base


def test_merge_company_location_inserts_new(monkeypatch):
    rows = []
    calls = []
    monkeypatch.setattr(db, "get_company_locations", lambda cid: list(rows))

    def fake_insert(contact_id, fields):
        calls.append(("insert", fields))
        rows.append(dict(fields, id="new", contact_id=contact_id))

    monkeypatch.setattr(db, "insert_company_location", fake_insert)
    monkeypatch.setattr(
        db, "update_company_location",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not update")))
    monkeypatch.setattr(
        db, "set_primary_location",
        lambda cid, location: calls.append(("primary", location["id"])))

    primary = db.merge_company_location("c1", _candidate(), "Kolkata")

    assert primary["id"] == "new"
    assert [c[0] for c in calls] == ["insert", "primary"]
    assert calls[-1] == ("primary", "new")


def test_merge_company_location_folds_repeat_sighting(monkeypatch):
    rows = [dict(_candidate(source_type="MIGRATED", status="APPROXIMATE",
                            evidence_saved=False, address_line2=""),
                 id="old", contact_id="c1")]
    updated = {}

    monkeypatch.setattr(db, "get_company_locations", lambda cid: list(rows))

    def fake_update(location_id, fields):
        updated.update(fields)
        rows[0].update(fields)

    monkeypatch.setattr(db, "update_company_location", fake_update)
    monkeypatch.setattr(
        db, "insert_company_location",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not insert")))
    monkeypatch.setattr(db, "set_primary_location", lambda *a: None)

    primary = db.merge_company_location(
        "c1", _candidate(address_line2="Park Street"), "Kolkata")

    assert len(rows) == 1
    assert primary["id"] == "old"
    assert updated["found_count"] == 2
    assert updated["source_type"] == "OWN_SITE"
    assert updated["status"] == "VERIFIED"
    assert updated["address_line2"] == "Park Street"

