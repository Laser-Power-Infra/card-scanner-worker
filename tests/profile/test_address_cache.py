"""Temporary JSON cache tests. Delete with app/profile/address_cache.py."""
import json

import pytest

from app.profile import address_cache, config


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ADDRESS_CACHE", "1")
    monkeypatch.setattr(config, "ADDRESS_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ADDRESS_CACHE_TTL_HOURS", 72)
    monkeypatch.setattr(config, "ADDRESS_FAILURE_TTL_HOURS", 12)
    yield tmp_path
    address_cache.clear()


def test_company_pages_round_trip():
    pages = [{"url": "https://acme.example/contact", "title": "contact",
              "text": "12 Park Street Kolkata 700022", "links": [],
              "company_trust": "", "company_tier": "high"}]
    address_cache.save_company("Acme", "https://acme.example", pages,
                               search_urls=["https://directory.example/acme"])
    cached = address_cache.load_company("Acme", "https://acme.example")
    assert cached["pages"][0]["text"] == "12 Park Street Kolkata 700022"
    assert cached["search_urls"] == ["https://directory.example/acme"]
    assert cached["pages"][0]["company_tier"] == "high"


def test_stale_company_pages_are_ignored(_cache_dir, monkeypatch):
    pages = [{"url": "https://acme.example/contact", "title": "contact",
              "text": "12 Park Street Kolkata 700022", "links": []}]
    address_cache.save_company("Acme", "https://acme.example", pages)
    monkeypatch.setattr(config, "ADDRESS_CACHE_TTL_HOURS", -1)
    # Rewrite with the now-negative TTL to make the entry stale.
    address_cache.save_company("Acme", "https://acme.example", pages)
    assert address_cache.load_company("Acme", "https://acme.example") is None


def test_deterministic_failure_is_reused_and_transient_is_not():
    address_cache.save_failure("Acme", "", "postcode_mismatch", "c1")
    assert address_cache.load_failure("Acme")["reason"] == "postcode_mismatch"

    address_cache.clear("Acme")
    address_cache.save_failure("Acme", "", "rate_limit", "c1")
    assert address_cache.load_failure("Acme") is None


def test_successful_run_can_clear_failure():
    address_cache.save_failure("Acme", "", "no_pages", "c1")
    address_cache.clear_failure("Acme")
    assert address_cache.load_failure("Acme") is None


def test_geocode_metadata_round_trip_keeps_precision_fields():
    result = {"lat": 22.5, "lon": 88.3, "result_type": "building",
              "rank": {"match_type": "full_match", "confidence": 0.9}}
    address_cache.put_geocode("Acme", "", "12 park street", result)
    cached = address_cache.get_geocode("Acme", "", "12 park street")
    assert cached["result_type"] == "building"
    assert cached["rank"]["confidence"] == 0.9


def test_corrupt_file_is_ignored(_cache_dir):
    key = address_cache._key("Acme", "")
    (_cache_dir / ("%s.json" % key)).write_text("{not json", encoding="utf-8")
    assert address_cache.load_company("Acme", "") is None
    assert address_cache.load_failure("Acme") is None


def test_disabled_cache_never_writes(_cache_dir, monkeypatch):
    monkeypatch.setenv("ADDRESS_CACHE", "0")
    address_cache.save_company("Acme", "", [{"url": "u", "text": "x"}])
    assert list(_cache_dir.glob("*.json")) == []


def test_old_cache_version_is_ignored(_cache_dir):
    key = address_cache._key("Acme", "")
    (_cache_dir / ("%s.json" % key)).write_text(json.dumps({
        "key": key,
        "version": "older",
        "pages": {"items": [{"url": "u", "text": "x"}],
                  "expires_at": "2999-01-01T00:00:00"},
    }), encoding="utf-8")
    assert address_cache.load_company("Acme", "") is None


def test_key_is_stable_for_case_and_www():
    assert address_cache._key("Acme Ltd", "https://www.acme.example") == \
        address_cache._key("acme ltd", "acme.example")


def test_saved_json_is_valid_and_uses_cleaned_text():
    address_cache.save_company(
        "Acme", "", [{"url": "https://acme.example", "title": "/",
                      "text": "12 Park Street, Kolkata 700022", "links": []}])
    path = next(address_cache._cache_dir().glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pages"]["items"][0]["text"] == "12 Park Street, Kolkata 700022"
