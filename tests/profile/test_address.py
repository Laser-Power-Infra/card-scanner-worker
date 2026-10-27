"""Behaviour tests for the address worker's exit points (no network/DB)."""
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.profile.workers import address
from app.profile import locations


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
    "address_type": "head_office",
    "evidence_url": "https://acme.example/contact",
    "quote": "Registered office address 12 Park Street Kolkata 700022",
}

_PAGES = [{"title": "contact", "url": "https://acme.example/contact",
           "text": "Registered office address 12 Park Street Kolkata 700022"}]


def _wire(monkeypatch, caplog, *, company="Acme", company_location="West Bengal",
          pages=None, extracted=_EXTRACTED, cached=None, budget_ok=True,
          geo_result="default", retrieve_error=None, area=None):
    caplog.set_level(logging.INFO, logger="card-desk.address-attempt")
    writes = {"failed": 0, "locations": [], "cache": 0}

    monkeypatch.setattr(address.db, "address_schema_ok", lambda: True)
    monkeypatch.setattr(address.db, "location_cache_schema_ok", lambda: True)
    monkeypatch.setattr(address.db, "company_location_schema_ok", lambda: True)
    monkeypatch.setattr(address, "_load_contact",
                        lambda cid, snap: (company, company_location,
                                           "acme.example", ""))
    monkeypatch.setattr(address.db, "get_location_cache", lambda key: cached)
    monkeypatch.setattr(address, "_area_point",
                        lambda extracted, notes, country_filter="in": area)

    def merge_location(contact_id, candidate, hint=None):
        writes["locations"].append(candidate)
        primary = locations.choose_primary(writes["locations"], hint)
        return dict(primary or candidate, id="loc-1")

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
    # The worker's second-pass search must not reach the live providers in a
    # unit test, even when _retrieve is faked.
    monkeypatch.setattr(address, "_search_pages", lambda *a, **k: [])

    def fake_extract(*args, **kwargs):
        if extracted is None:
            return []
        return extracted if isinstance(extracted, list) else [extracted]

    monkeypatch.setattr(address.address_ai, "extract_company_addresses",
                        fake_extract)
    monkeypatch.setattr(address.budget, "reserve", lambda: budget_ok)
    fake_result = _geo_result() if geo_result == "default" else geo_result
    monkeypatch.setattr(
        address.geo, "geocode_many",
        lambda query, limit=5, country_filter="in": [] if fake_result is None else [fake_result])
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
    assert result["reason"] == "no_street_line"
    assert result["data"]["addresses"] == [{"city": "Kolkata"}]
    assert result["data"]["rejected"][0]["reason"] == "no_street_line"
    assert _recorded_reason(caplog) == "no_street_line"


def test_empty_retrieval_records_no_pages(monkeypatch, caplog):
    _wire(monkeypatch, caplog)

    def retrieve(company, location, site, official, notes=None):
        notes.append("company_filter_fallback: no page names Acme")
        return []

    monkeypatch.setattr(address, "_retrieve", retrieve)
    result = _run()
    assert result["reason"] == "no_pages"
    assert _recorded_reason(caplog) == "no_pages"


def test_country_mismatch_stops_simpler_lookups(monkeypatch, caplog):
    _wire(monkeypatch, caplog, area=None)
    calls = []

    def fake_geocode(query, limit=5, country_filter="in"):
        calls.append(query)
        return [_geo_result(country_code="us")]

    monkeypatch.setattr(address.geo, "geocode_many", fake_geocode)
    result = _run()
    assert result["reason"] == "country_mismatch"
    assert len(calls) == 1
    assert "full lookup permanent failure: country_mismatch" in caplog.text


def test_international_contact_uses_expected_country(monkeypatch, caplog):
    extracted = {
        "address_line1": "1 Sheikh Zayed Road", "address_line2": "",
        "city": "Dubai", "state": "", "postal_code": "00000",
        "country": "", "address_type": "head_office",
        "evidence_url": "https://acme.example/contact",
        "quote": "Office 1 Sheikh Zayed Road Dubai",
    }
    pages = [{"url": "https://acme.example/contact", "title": "contact",
              "text": "Office 1 Sheikh Zayed Road Dubai 00000"}]
    geo_result = _geo_result(
        country_code="ae", country="United Arab Emirates", state="",
        state_code="", city="Dubai", street="Sheikh Zayed Road",
        housenumber="1", postcode="00000",
        formatted="1 Sheikh Zayed Road, Dubai, United Arab Emirates")
    writes = _wire(monkeypatch, caplog, company="Acme",
                   company_location="Dubai", extracted=extracted,
                   pages=pages, geo_result=geo_result)
    result = _run()
    assert result["reason"] == "verified"
    assert result["country"] == "United Arab Emirates"
    assert writes["locations"][0]["country"] == "United Arab Emirates"
    assert "international contact: expected country ae" in caplog.text


def test_missing_country_inferred_for_indian_pin(monkeypatch, caplog):
    extracted = dict(_EXTRACTED, country="")
    writes = _wire(monkeypatch, caplog, extracted=extracted)
    result = _run()
    assert result["reason"] == "verified"
    assert writes["locations"][0]["country"] == "India"


def test_quote_not_on_page_is_rejected(monkeypatch, caplog):
    extracted = dict(_EXTRACTED, quote="this sentence is not on the page")
    writes = _wire(monkeypatch, caplog, extracted=extracted)
    result = _run()
    assert result["reason"] == "quote_not_found"
    assert result["data"]["rejected"][0]["reason"] == "quote_not_found"
    assert writes["locations"] == []
    assert writes["failed"] == 1


def test_extraction_failure_is_not_cached_or_counted(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog)

    def boom(*args, **kwargs):
        raise address.address_ai.ExtractionError("permanent", "401")

    monkeypatch.setattr(address.address_ai,
                        "extract_company_addresses", boom)
    result = _run()
    assert result["reason"] == "extraction_failed"
    assert writes["failed"] == 0
    assert writes["locations"] == []


def test_head_office_preferred_over_branch(monkeypatch, caplog):
    branch = dict(_EXTRACTED, address_type="branch",
                  address_line1="5 Branch Road",
                  quote="Branch office 5 Branch Road Kolkata 700022")
    pages = [{"url": "https://acme.example/contact",
              "title": "contact",
              "text": "Registered office address 12 Park Street Kolkata 700022. "
                      "Branch office 5 Branch Road Kolkata 700022."}]
    writes = _wire(monkeypatch, caplog, pages=pages,
                   extracted=[branch, dict(_EXTRACTED)])
    result = _run()
    assert result["address_line1"] == "12 Park Street"
    assert writes["locations"][0]["address_line1"] == "12 Park Street"
    assert "other address: branch 5 Branch Road" in caplog.text


def test_po_box_is_stripped_before_geocode(monkeypatch, caplog):
    extracted = dict(_EXTRACTED, address_line2="P.B. No. 1568")
    writes = _wire(monkeypatch, caplog, extracted=extracted)
    calls = []

    def fake_geocode(query, limit=5, country_filter="in"):
        calls.append(query)
        return [_geo_result()]

    monkeypatch.setattr(address.geo, "geocode_many", fake_geocode)
    result = _run()
    assert result["reason"] == "verified"
    assert all("P.B." not in query for query in calls)
    assert writes["locations"][0]["address_line2"] == ""


def test_database_location_cache_is_not_used_for_verification(monkeypatch, caplog):
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
    # The worker now uses the full filesystem geocode cache (with precision)
    # instead of trusting LocationCache, which has no precision metadata.
    assert result["reason"] == "verified"
    assert len(writes["locations"]) == 1
    assert writes["locations"][0]["precision"] == "STREET"
    assert writes["cache"] == 1


def test_budget_exhausted_records_without_attempt(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog, budget_ok=False)
    result = _run()
    assert result["reason"] == "budget_exhausted"
    assert writes["failed"] == 0
    assert _recorded_reason(caplog) == "budget_exhausted"


def test_postcode_mismatch_is_rejected(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog,
                   geo_result=_geo_result(postcode="700001", city="Howrah"))
    result = _run()
    assert result["ok"] is False
    assert result["reason"] == "postcode_mismatch"
    assert "outcome   : FAIL" in caplog.text
    assert "[FAIL] postcode:" in caplog.text
    assert "[FAIL] city:" in caplog.text
    assert writes["locations"] == []
    assert writes["failed"] == 1
    assert _recorded_reason(caplog) == "postcode_mismatch"


def test_low_confidence_is_saved_approximate(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog,
                   geo_result=_geo_result(rank={"confidence": 0.2,
                                                "match_type": "full_match"}))
    result = _run()
    assert result["ok"] is True
    assert result["reason"] == "approximate"
    assert writes["locations"][0]["status"] == "APPROXIMATE"


def test_area_distance_does_not_override_hard_city_postcode(monkeypatch, caplog):
    _wire(monkeypatch, caplog, area=(22.5, 88.3),
          geo_result=_geo_result(postcode="700001", city="Howrah"))
    result = _run()
    assert result["reason"] == "postcode_mismatch"
    assert "[FAIL] postcode:" in caplog.text
    assert "[PASS] area_distance:" in caplog.text


def test_wrong_country_is_rejected(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog,
                   geo_result=_geo_result(country_code="us"))
    result = _run()
    assert result["ok"] is False
    assert result["reason"] == "country_mismatch"
    assert writes["locations"] == []
    assert writes["failed"] == 1
    assert "outcome   : FAIL" in caplog.text


def test_rejection_without_street_line_is_not_saved(monkeypatch, caplog):
    extracted = dict(_EXTRACTED, address_line1="", address_line2="NCC House")
    writes = _wire(monkeypatch, caplog, extracted=extracted,
                   geo_result=_geo_result(result_type="city", street=None,
                                          housenumber=None))
    result = _run()
    assert result["ok"] is False
    assert result["reason"] == "no_street_line"
    assert result["data"]["rejected"][0]["reason"] == "no_street_line"
    assert writes["locations"] == []
    assert writes["failed"] == 1


def test_trusted_registry_is_saved_without_map_result(monkeypatch, caplog):
    registry_url = "https://gst.example/gstin/36ABCDE1234F1Z5"
    pages = [{"url": registry_url, "title": "gst",
              "text": "Registered office address 12 Park Street Kolkata 700022"}]
    extracted = dict(_EXTRACTED, evidence_url=registry_url)
    writes = _wire(monkeypatch, caplog, extracted=extracted, pages=pages,
                   geo_result=None)
    result = _run()
    assert result["reason"] == "approximate"
    assert result["address_status"] == "approximate"
    assert "trusted source saved without a map result" in caplog.text
    assert len(writes["locations"]) == 1
    assert writes["locations"][0]["source_type"] == "REGISTRY"
    assert writes["locations"][0]["status"] == "APPROXIMATE"
    assert writes["locations"][0]["precision"] == "CITY"


def test_fallback_lookup_stops_on_street_postcode(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog)
    calls = []

    def fake(query, limit=5, country_filter="in"):
        calls.append(query)
        return [] if len(calls) == 1 else [_geo_result()]

    monkeypatch.setattr(address.geo, "geocode_many", fake)
    result = _run()
    assert result["reason"] == "verified"
    assert len(calls) == 2
    assert "Kolkata" in calls[0]
    assert "Kolkata" not in calls[1]
    assert "full:0/no_results | street_postcode:1/verified" in caplog.text
    assert "worked: street_postcode" in caplog.text
    assert len(writes["locations"]) == 1


def test_stale_database_cache_does_not_block_fresh_lookup(monkeypatch, caplog):
    cached = {
        "placeId": "cached-pid",
        "latitude": 19.0,
        "longitude": 73.0,
        "formattedAddress": "Wrong place",
        "expiresAt": datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(days=1),
        "resolved": True,
    }
    _wire(monkeypatch, caplog, cached=cached, area=(22.5, 88.3))
    result = _run()
    assert result["reason"] == "verified"
    assert "area from pin 700022" in caplog.text or "full:1/verified" in caplog.text


def test_verified_keeps_extracted_parts_and_map_coordinates(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog,
                   geo_result=_geo_result(street="Park Street",
                                          housenumber="99",
                                          address_line1="99 Park Street"))
    result = _run()
    assert result["reason"] == "verified"
    assert result["address_line1"] == "12 Park Street"
    assert writes["locations"][0]["address_line1"] == "12 Park Street"
    assert writes["locations"][0]["latitude"] == 22.5
    assert writes["locations"][0]["longitude"] == 88.3


def test_verified_requires_street_overlap(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog,
                   geo_result=_geo_result(street="Some Other Road",
                                          housenumber="99",
                                          address_line1="99 Some Other Road"))
    result = _run()
    assert result["reason"] == "approximate"
    assert writes["locations"][0]["status"] == "APPROXIMATE"


def test_verified_requires_half_km(monkeypatch, caplog):
    writes = _wire(monkeypatch, caplog, area=(22.51, 88.3),
                   geo_result=_geo_result())
    result = _run()
    assert result["reason"] == "approximate"
    assert writes["locations"][0]["status"] == "APPROXIMATE"


def test_two_source_upgrade_reports_verified(monkeypatch, caplog):
    _wire(monkeypatch, caplog,
          geo_result=_geo_result(rank={"confidence": 0.2,
                                       "match_type": "full_match"}))
    monkeypatch.setattr(
        address.db, "merge_company_location",
        lambda cid, candidate, hint=None: dict(candidate, id="loc-1",
                                               status="VERIFIED"))
    result = _run()
    assert result["reason"] == "verified"
    assert result["address_status"] == "verified"


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


def test_area_level_geocode_is_saved_with_city_precision(monkeypatch, caplog):
    writes = _wire(
        monkeypatch, caplog,
        geo_result=_geo_result(result_type="city", street=None,
                               housenumber=None))
    result = _run()
    assert result["reason"] == "approximate"
    assert writes["locations"][0]["precision"] == "CITY"


def test_low_trust_pages_cannot_be_saved_verified(monkeypatch, caplog):
    pages = [dict(_PAGES[0], company_trust="low")]
    writes = _wire(monkeypatch, caplog, pages=pages)
    result = _run()
    assert result["ok"] is True
    assert result["address_status"] == "approximate"
    assert writes["locations"][0]["status"] == "APPROXIMATE"
    assert "low_company_match" in caplog.text


def test_multiple_offices_are_saved_and_in_state_office_is_primary(monkeypatch,
                                                                  caplog):
    vijayawada = dict(_EXTRACTED, address_type="registered",
                      address_line1="32-35-54 Sriven Residency",
                      city="Vijayawada", state="Andhra Pradesh",
                      postal_code="520004",
                      quote="Registered Office 32-35-54 Sriven Residency "
                            "Vijayawada 520004")
    hyderabad = dict(_EXTRACTED, address_type="branch",
                     address_line1="Kolluri house 8-3-318",
                     city="Hyderabad", state="Telangana", postal_code="500081",
                     quote="Hyderabad Office Kolluri house 8-3-318 "
                           "Hyderabad 500081")
    pages = [{"url": "https://acme.example/contact", "title": "contact",
              "text": "Registered Office 32-35-54 Sriven Residency Vijayawada "
                      "520004. Hyderabad Office Kolluri house 8-3-318 "
                      "Hyderabad 500081."}]

    def fake_geocode(query, limit=5, country_filter="in"):
        if "vijayawada" in query.lower() or "520004" in query:
            return [_geo_result(city="Vijayawada", state="Andhra Pradesh",
                                state_code="AP", postcode="520004",
                                street="Sriven Residency", housenumber="32-35-54",
                                lat=16.52, lon=80.66)]
        return [_geo_result(city="Hyderabad", state="Telangana",
                            state_code="TG", postcode="500081",
                            street="Kolluri house", housenumber="8-3-318",
                            lat=17.44, lon=78.38)]

    writes = _wire(monkeypatch, caplog, pages=pages,
                   extracted=[vijayawada, hyderabad],
                   company_location="Telangana")
    monkeypatch.setattr(address.geo, "geocode_many", fake_geocode)
    result = _run()
    assert result["ok"] is True
    assert len(writes["locations"]) == 2
    assert result["city"] == "Hyderabad"
    assert any(loc["city"] == "Vijayawada" for loc in writes["locations"])
    assert any(loc["city"] == "Hyderabad" for loc in writes["locations"])


def test_failure_is_reused_within_a_run(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("ADDRESS_CACHE", "1")
    monkeypatch.setattr(address.config, "ADDRESS_CACHE_DIR", str(tmp_path))
    calls = {"retrieve": 0}
    writes = _wire(monkeypatch, caplog, pages=[])

    def retrieve(*args, **kwargs):
        calls["retrieve"] += 1
        return []

    monkeypatch.setattr(address, "_retrieve", retrieve)
    first = _run()
    second = _run()
    assert first["reason"] == "no_pages"
    assert second["reason"] == "no_pages"
    assert calls["retrieve"] == 1
    assert "reused failure" in caplog.text
    address.address_cache.clear()
