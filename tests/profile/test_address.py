"""Behaviour tests for the address worker's exit points (no network/DB)."""
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.profile.workers import address


def _geo_result(**overrides):
    result = {
        "country_code": "in",
        "state": "West Bengal",
        "state_code": "WB",
        "city": "Kolkata",
        "street": "Park Street",
        "housenumber": "12",
        "postcode": "700022",
        "country": "India",
        "formatted": "12 Park Street, Kolkata, West Bengal 700022, India",
        "result_type": "building",
        "rank": {"confidence": 1.0, "match_type": "full_match"},
        "lat": 22.5,
        "lon": 88.3,
        "place_id": "pid",
    }
    result.update(overrides)
    return result


_EXTRACTED = {
    "address_line1": "12 Park Street",
    "address_line2": "",
    "city": "Kolkata",
    "state": "West Bengal",
    "postal_code": "700022",
    "country": "India",
    "evidence_url": "https://acme.example/contact",
}

_PAGES = [{"title": "contact", "url": "https://acme.example/contact",
           "text": "Registered office address 12 Park Street Kolkata 700022"}]


def _wire(monkeypatch, caplog, *, company="Acme", company_location="West Bengal",
          pages=None, extracted=_EXTRACTED, cached=None, budget_ok=True,
          geo_result=None, retrieve_error=None):
    caplog.set_level(logging.INFO, logger="card-desk.address-attempt")
    writes = {"failed": 0, "locations": [], "cache": 0}

    monkeypatch.setattr(address.db, "address_schema_ok", lambda: True)
    monkeypatch.setattr(address.db, "location_cache_schema_ok", lambda: True)
    monkeypatch.setattr(address.db, "company_location_schema_ok", lambda: True)
    monkeypatch.setattr(address, "_load_contact",
                        lambda cid, snap: (company, company_location,
                                           "acme.example", ""))
    monkeypatch.setattr(address.db, "get_location_cache", lambda key: cached)

    def merge_location(contact_id, candidate, hint=None):
        writes["locations"].append(candidate)
        return dict(candidate, id="loc-1")

    monkeypatch.setattr(address.db, "merge_company_location", merge_location)
    monkeypatch.setattr(address.db, "set_company_location_if_empty",
                        lambda *a: None)
    monkeypatch.setattr(
        address.db, "upsert_location_cache",
        lambda *a: writes.__setitem__("cache", writes["cache"] + 1))
    monkeypatch.setattr(
        address.db, "mark_address_failed",
        lambda cid: writes.__setitem__("failed", writes["failed"] + 1))

    def retrieve(*args, **kwargs):
        if retrieve_error is not None:
            raise retrieve_error
        return _PAGES if pages is None else pages

    monkeypatch.setattr(address, "_retrieve", retrieve)
    monkeypatch.setattr(address.address_ai, "extract_company_address",
                        lambda *a, **k: extracted)
    monkeypatch.setattr(address.budget, "reserve", lambda: budget_ok)
    monkeypatch.setattr(address.geo, "geocode",
                        lambda addr: _geo_result() if geo_result is None else geo_result)
    return writes


def _run():
    return address.run({"contactId": "c1", "tag": "address", "snapshot": {}})


def _recorded_reason(caplog):
    match = re.search(r"reason\s+:\s*(\S+)", caplog.text)
    return match.group(1) if match else None


def test_no_company_records_and_fails(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog, company="")
    result = _run()
    assert result["reason"] == "no_company"
    assert writes["failed"] == 1
    assert _recorded_reason(caplog) == "no_company"


def test_no_pages_records_and_fails(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog, pages=[])
    result = _run()
    assert result["reason"] == "no_pages"
    assert writes["failed"] == 1
    assert _recorded_reason(caplog) == "no_pages"


def test_incomplete_extraction_records_data(monkeypatch, caplog):
    _wire(monkeypatch, caplog, extracted={"city": "Kolkata"})
    result = _run()
    assert result["reason"] == "incomplete_extraction"
    assert result["data"] == {"city": "Kolkata"}
    assert _recorded_reason(caplog) == "incomplete_extraction"


def test_cache_hit_records_pass(monkeypatch, caplog):
    cached = {
        "placeId": "cached-pid",
        "latitude": 22.5,
        "longitude": 88.3,
        "formattedAddress": "12 Park Street, Kolkata 700022",
        "expiresAt": datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(days=1),
        "resolved": True,
    }
    writes = _wire(monkeypatch, caplog, cached=cached)
    result = _run()
    assert result["reason"] == "cache_hit"
    assert len(writes["locations"]) == 1
    assert writes["locations"][0]["latitude"] == 22.5
    assert writes["locations"][0]["status"] == "VERIFIED"
    assert _recorded_reason(caplog) == "cache_hit"
    assert "outcome   : PASS" in caplog.text
    assert "type=cached" in caplog.text


def test_budget_exhausted_records_without_attempt(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog, budget_ok=False)
    result = _run()
    assert result["reason"] == "budget_exhausted"
    assert writes["failed"] == 0
    assert _recorded_reason(caplog) == "budget_exhausted"


def test_rejection_records_every_failed_check(monkeypatch, caplog):
    _wire(monkeypatch, caplog, geo_result=_geo_result(postcode="700001",
                                                      city="Howrah"))
    result = _run()
    assert result["reason"] == "postcode_mismatch"
    assert "outcome   : FAIL" in caplog.text
    assert "[FAIL] postcode:" in caplog.text
    assert "[FAIL] city:" in caplog.text


def test_verified_records_pass_and_warms_cache(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog)
    result = _run()
    assert result["reason"] == "verified"
    assert result["city"] == "Kolkata"
    assert writes["cache"] == 1
    assert len(writes["locations"]) == 1
    saved = writes["locations"][0]
    assert saved["source_type"] == "OWN_SITE"
    assert saved["precision"] == "STREET"
    assert saved["status"] == "VERIFIED"
    assert saved["evidence_saved"] is True
    assert _recorded_reason(caplog) == "verified"
    assert "outcome   : PASS" in caplog.text


def test_unexpected_exception_is_recorded_then_reraised(monkeypatch, caplog):
    _wire(monkeypatch, caplog, retrieve_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        _run()
    assert _recorded_reason(caplog) == "unhandled_exception"
