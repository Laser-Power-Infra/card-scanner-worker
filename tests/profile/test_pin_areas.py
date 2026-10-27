"""Tests for the Geoapify-backed PIN centre cache."""
import json

from app.profile import geo, pin_areas


def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("PIN_AREAS_FILE", str(tmp_path / "pin_areas.json"))
    monkeypatch.setattr(pin_areas, "_data", None)


def test_geoapify_result_is_cached_and_only_requested_once(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    calls = []

    def fake_postcode(pin):
        calls.append(pin)
        return [{"lat": 22.5853, "lon": 88.4864, "city": "Bidhannagar",
                 "district": "North 24 Parganas", "state": "West Bengal"}]

    monkeypatch.setattr(geo, "geocode_postcode", fake_postcode)

    first = pin_areas.lookup("700 160")
    second = pin_areas.lookup("700160")

    assert first["valid"] is True
    assert first["source"] == "geoapify"
    assert first["lat"] == 22.5853
    assert second["source"] == "geoapify"
    assert calls == ["700160"]
    saved = json.loads((tmp_path / "pin_areas.json").read_text(encoding="utf-8"))
    assert saved["pins"]["700160"]["city"] == "Bidhannagar"


def test_geonames_fallback_is_labelled(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    def failing(pin):
        raise geo.GeoTransient("down")

    monkeypatch.setattr(geo, "geocode_postcode", failing)
    row = pin_areas.lookup("700160")
    assert row["valid"] is True
    assert row["source"] == "geonames"
    assert row["lat"] is not None


def test_unknown_pin_never_calls_geoapify(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(geo, "geocode_postcode",
                        lambda pin: calls.append(pin) or [])
    row = pin_areas.lookup("000000")
    assert row["valid"] is False
    assert calls == []


def test_city_matches_uses_city_or_district_and_tolerates_missing(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(geo, "geocode_postcode", lambda pin: [])
    monkeypatch.setattr(pin_areas, "_data", {"pins": {"700160": {
        "pin": "700160", "lat": 22.5, "lon": 88.5,
        "city": "Bidhannagar", "district": "North 24 Parganas",
        "state": "West Bengal", "source": "geoapify",
        "fetched_at": "2026-01-01T00:00:00"}}})
    assert pin_areas.city_matches("700160", "Kolkata")
    assert pin_areas.city_matches("700160", "Salt Lake")
    assert not pin_areas.city_matches("700160", "Mumbai")
    assert pin_areas.city_matches("700160", "")
    assert pin_areas.city_matches("", "Mumbai")

def test_city_matches_extracts_known_city_from_locality(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(pin_areas, "_data", {"pins": {"700061": {
        "pin": "700061", "lat": 22.48, "lon": 88.28,
        "city": "Kolkata", "district": "Kolkata", "state": "West Bengal",
        "source": "geoapify", "fetched_at": "2026-01-01T00:00:00"}}})
    assert pin_areas.city_matches("700061", "New Town, (N) 24 Parganas")
