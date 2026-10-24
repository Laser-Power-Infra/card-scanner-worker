from app.profile import geo


def _result(**overrides):
    result = {
        "country_code": "in",
        "state": "Maharashtra",
        "state_code": "MH",
        "city": "Pune",
        "street": "MG Road",
        "housenumber": "10",
        "postcode": "411001",
        "country": "India",
        "address_line1": "10 MG Road",
        "formatted": "10 MG Road, Pune, Maharashtra 411001, India",
        "result_type": "building",
        "rank": {"confidence": 1.0, "match_type": "full_match"},
        "lat": 18.5,
        "lon": 73.8,
        "place_id": "pid",
    }
    result.update(overrides)
    return result


def test_accept_happy_path():
    ok, reason = geo.accept(_result(), "Maharashtra")
    assert ok is True
    assert reason == "ok"


def test_accept_rejects_empty():
    ok, reason = geo.accept(None, None)
    assert ok is False
    assert reason == "no_results"


def test_accept_rejects_non_india():
    ok, reason = geo.accept(_result(country_code="us"), None)
    assert ok is False
    assert reason == "country_mismatch"


def test_accept_rejects_coarse_result_type():
    ok, reason = geo.accept(_result(result_type="city", street=None), None)
    assert ok is False
    assert reason == "not_street_level"


def test_accept_rejects_weak_match():
    ok, reason = geo.accept(
        _result(rank={"confidence": 1.0,
                      "match_type": "match_by_city_or_district"}), None)
    assert ok is False
    assert reason == "weak_match"


def test_accept_rejects_low_confidence():
    ok, reason = geo.accept(
        _result(rank={"confidence": 0.2, "match_type": "full_match"}), None)
    assert ok is False
    assert reason == "low_confidence"


def test_accept_rejects_missing_postcode():
    ok, reason = geo.accept(_result(postcode=None), None)
    assert ok is False
    assert reason == "not_street_level"


def test_accept_rejects_state_mismatch():
    ok, reason = geo.accept(_result(), "Karnataka")
    assert ok is False
    assert reason == "state_mismatch"


def test_accept_rejects_postcode_mismatch():
    ok, reason = geo.accept(_result(), None,
                             {"postal_code": "411002", "city": "Pune",
                              "address_line1": "10 MG Road"})
    assert ok is False
    assert reason == "postcode_mismatch"


def test_accept_rejects_street_mismatch():
    ok, reason = geo.accept(_result(), None,
                             {"postal_code": "411001", "city": "Pune",
                              "address_line1": "407 Raheja Centre"})
    assert ok is False
    assert reason == "street_mismatch"


def test_accept_expected_match_passes():
    ok, reason = geo.accept(_result(), None,
                             {"postal_code": "411001", "city": "Pune",
                              "address_line1": "10 MG Road"})
    assert ok is True
    assert reason == "ok"


def test_extract_prefers_street_address_over_poi_name():
    fields = geo.extract_fields(_result(address_line1="Some Hospital"))
    assert fields["address_line1"] == "10 MG Road"


def test_extract_fields_flattens():
    fields = geo.extract_fields(_result())
    assert fields["address_line1"] == "10 MG Road"
    assert fields["city"] == "Pune"
    assert fields["state"] == "Maharashtra"
    assert fields["postal_code"] == "411001"
    assert fields["country"] == "India"
    assert fields["latitude"] == 18.5
    assert fields["longitude"] == 73.8
    assert fields["place_id"] == "pid"
