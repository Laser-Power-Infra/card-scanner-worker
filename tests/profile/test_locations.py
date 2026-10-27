"""Pure tests for company-location identity and the primary rule."""
from app.profile import locations


def _loc(**overrides):
    base = {
        "id": "l1",
        "address_line1": "12 Park Street",
        "address_line2": "",
        "city": "Kolkata",
        "state": "West Bengal",
        "postal_code": "700022",
        "country": "India",
        "latitude": 22.5,
        "longitude": 88.3,
        "source_type": "DIRECTORY",
        "status": "APPROXIMATE",
        "precision": "STREET",
        "found_count": 1,
        "evidence_saved": True,
    }
    base.update(overrides)
    return base


def test_haversine_is_metres():
    assert 100 < locations.haversine_m(22.5, 88.3, 22.501, 88.3) < 115


def test_same_location_within_200m():
    assert locations.same_location(
        _loc(), _loc(address_line1="Other Road", latitude=22.501))


def test_different_location_beyond_200m():
    assert not locations.same_location(
        _loc(), _loc(address_line1="Other Road", latitude=22.53))


def test_same_location_by_street_and_postcode_without_coords():
    assert locations.same_location(
        _loc(latitude=None, longitude=None),
        _loc(latitude=None, longitude=None, address_line1="12 park street"))


def test_same_street_different_postcode_is_new():
    assert not locations.same_location(
        _loc(latitude=None, longitude=None),
        _loc(latitude=None, longitude=None, postal_code="700001"))


def test_source_type_prefers_own_site():
    assert locations.source_type(
        "https://www.acme.example/contact", "https://acme.example") == "OWN_SITE"


def test_source_type_registry_and_directory():
    assert locations.source_type("https://gst.jamku.app/gstin/36X") == "REGISTRY"
    assert locations.source_type(
        "https://www.figmentglobal.com/gst-number-search/gstin/19AAGFF") == "REGISTRY"
    assert locations.source_type("https://www.zaubacorp.com/company/X") == "DIRECTORY"


def test_precision_levels():
    assert locations.precision({"address_line1": "12 Park Street"}) == "STREET"
    assert locations.precision({"address_line2": "Park Street"}) == "AREA"
    assert locations.precision({"city": "Kolkata"}) == "CITY"


def test_build_location_marks_evidence():
    built = locations.build_location(
        {"address_line1": "12 Park Street", "city": "Kolkata",
         "latitude": 22.5, "longitude": 88.3},
        source_url="https://acme.example/contact", website="acme.example",
        status="VERIFIED")
    assert built["source_type"] == "OWN_SITE"
    assert built["precision"] == "STREET"
    assert built["status"] == "VERIFIED"
    assert built["evidence_saved"] is True
    assert built["source_urls"] == ["https://acme.example/contact"]
    assert built["found_count"] == 1


def test_merge_details_fills_gaps_and_counts():
    existing = _loc(address_line2="", source_type="MIGRATED",
                    status="APPROXIMATE", evidence_saved=False,
                    source_urls=[])
    candidate = _loc(address_line2="Park Street", source_type="OWN_SITE",
                     status="VERIFIED", evidence_saved=True,
                     source_url="https://acme.example/contact")
    merged = locations.merge_details(existing, candidate)
    assert merged["address_line2"] == "Park Street"
    assert merged["source_type"] == "OWN_SITE"
    assert merged["status"] == "VERIFIED"
    assert merged["evidence_saved"] is True
    assert merged["found_count"] == 2


def test_merge_cleans_po_box_and_uses_latest_parts():
    existing = _loc(address_line1="5-1-503, Ranigunj",
                    address_line2="P.B. No. 1568")
    candidate = _loc(address_line1="Plot No 5-1-503, Ranigunj",
                     address_line2="")
    merged = locations.merge_details(existing, candidate)
    assert merged["address_line1"] == "Plot No 5-1-503, Ranigunj"
    assert merged["address_line2"] == ""


def test_two_distinct_sources_upgrade_to_verified():
    existing = _loc(source_urls=["https://directory.example/acme"],
                    source_type="DIRECTORY", status="APPROXIMATE")
    candidate = _loc(source_url="https://acme.example/contact",
                     source_type="DIRECTORY", status="APPROXIMATE")
    merged = locations.merge_details(existing, candidate)
    assert merged["status"] == "VERIFIED"
    assert len(merged["source_urls"]) == 2


def test_same_host_does_not_upgrade():
    existing = _loc(source_urls=["https://acme.example/about"],
                    status="APPROXIMATE")
    candidate = _loc(source_url="https://acme.example/contact",
                     status="APPROXIMATE")
    merged = locations.merge_details(existing, candidate)
    assert merged["status"] == "APPROXIMATE"


def test_primary_prefers_card_location_match_over_trust():
    own_site = _loc(id="own", source_type="OWN_SITE", state="Maharashtra")
    directory = _loc(id="card", source_type="DIRECTORY", state="West Bengal")
    primary = locations.choose_primary([own_site, directory], "West Bengal")
    assert primary["id"] == "card"


def test_primary_prefers_trusted_source():
    own_site = _loc(id="own", source_type="OWN_SITE", found_count=1)
    directory = _loc(id="dir", source_type="DIRECTORY", found_count=9)
    assert locations.choose_primary([own_site, directory], "")["id"] == "own"


def test_primary_prefers_most_found():
    first = _loc(id="a", found_count=1)
    second = _loc(id="b", found_count=3)
    assert locations.choose_primary([first, second], None)["id"] == "b"


def test_primary_empty():
    assert locations.choose_primary([], None) is None


def test_hint_rank_prefers_city_then_state():
    location = _loc(city="Hyderabad", state="Telangana")
    assert locations.hint_rank(location, "Hyderabad, Telangana") == 3
    assert locations.hint_rank(location, "Telangana") == 2
    assert locations.hint_rank(location, "West Bengal") == 0


def test_out_of_state_office_is_not_primary_when_in_state_exists():
    hyderabad = _loc(id="hyd", city="Hyderabad", state="Telangana",
                     status="APPROXIMATE", precision="AREA")
    vijayawada = _loc(id="vja", city="Vijayawada", state="Andhra Pradesh",
                      status="VERIFIED", precision="STREET")
    primary = locations.choose_primary([vijayawada, hyderabad], "Telangana")
    assert primary["id"] == "hyd"


def test_merge_upgrades_coarse_coordinates_when_precision_improves():
    existing = _loc(precision="CITY", latitude=22.4, longitude=88.2)
    candidate = _loc(precision="STREET", latitude=22.5, longitude=88.3)
    merged = locations.merge_details(existing, candidate)
    assert merged["precision"] == "STREET"
    assert merged["latitude"] == 22.5
    assert merged["longitude"] == 88.3


def test_merge_keeps_street_pin_when_new_candidate_is_coarser():
    existing = _loc(precision="STREET", latitude=22.5, longitude=88.3)
    candidate = _loc(precision="CITY", latitude=22.4, longitude=88.2)
    merged = locations.merge_details(existing, candidate)
    assert merged["precision"] == "STREET"
    assert merged["latitude"] == 22.5
    assert merged["longitude"] == 88.3
