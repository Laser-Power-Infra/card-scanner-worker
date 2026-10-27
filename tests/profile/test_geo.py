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


def test_check_all_reports_every_failure():
    # Deliberately mismatched geocode result vs extracted address: the point of
    # the test is that all four disagreements are reported, not just the first.
    mismatched = _result(postcode="411002", city="Mumbai", street="FC Road")
    expected = {"postal_code": "411001", "city": "Pune",
                "address_line1": "407 Raheja Centre"}

    checks = geo.check_all(mismatched, "Karnataka", expected)
    failed = {item["name"] for item in checks if not item["passed"]}
    assert failed == {"postcode", "city", "street", "state"}
    assert geo.decide(checks) == (False, "postcode_mismatch")
    assert geo.accept(mismatched, "Karnataka", expected) == (
        False, "postcode_mismatch")


def test_check_all_passes_clean_result():
    checks = geo.check_all(_result(), "Maharashtra")
    assert checks
    assert all(item["passed"] for item in checks)
    assert geo.decide(checks) == (True, "ok")


def test_check_all_records_no_results():
    checks = geo.check_all(None, None)
    assert [item["name"] for item in checks] == ["no_results"]
    assert geo.decide(checks) == (False, "no_results")


def test_result_summary_flattens():
    base = _result()
    summary = geo.result_summary(base)
    assert summary["formatted"] == base["formatted"]
    assert summary["lat"] == base["lat"]
    assert summary["lon"] == base["lon"]
    assert summary["resultType"] == base["result_type"]
    assert summary["matchType"] == base["rank"]["match_type"]
    assert summary["confidence"] == base["rank"]["confidence"]
    assert summary["placeId"] == base["place_id"]


def _expected(**overrides):
    base = {"postal_code": "411001", "city": "Pune",
            "state": "Maharashtra", "country": "India",
            "address_line1": "10 MG Road"}
    base.update(overrides)
    return base


def test_assess_hard_city_and_postcode_mismatch_beats_distance():
    verdict = geo.assess(
        _result(postcode="411002", city="Mumbai"), "Maharashtra",
        _expected(), area=(18.5, 73.8))
    assert verdict["outcome"] == "rejected"
    assert verdict["reason"] == "postcode_mismatch"
    assert verdict["distance_km"] == 0


def test_trusted_source_state_contradiction_is_approximate_review():
    verdict = geo.assess(
        _result(state="Andhra Pradesh", state_code="AP",
                city="Vijayawada", postcode="520004",
                lat=16.52, lon=80.66),
        "Telangana",
        _expected(state="Andhra Pradesh", city="Vijayawada",
                  postal_code="520004"),
        area=(16.52, 80.66), source_type="OWN_SITE")
    assert verdict["outcome"] == "approximate"
    assert "state_mismatch" in verdict["reason"]
    assert verdict["review_reasons"]


def test_directory_state_contradiction_is_rejected():
    verdict = geo.assess(
        _result(state="Andhra Pradesh", state_code="AP",
                city="Vijayawada", postcode="520004",
                lat=16.52, lon=80.66),
        "Telangana",
        _expected(state="Andhra Pradesh", city="Vijayawada",
                  postal_code="520004"),
        area=(16.52, 80.66), source_type="DIRECTORY")
    assert verdict["outcome"] == "rejected"
    assert verdict["reason"] == "state_mismatch"


def test_city_only_company_location_does_not_reject_correct_result():
    # A card that says "Pune" (a city) used to fail the state hint check and
    # reject a perfect result with state_mismatch.
    verdict = geo.assess(_result(), "Pune", _expected(),
                         area=(18.5, 73.8), source_type="DIRECTORY")
    assert verdict["outcome"] == "verified"
    assert verdict["reason"] == "ok"


def test_city_alias_resolves_to_state_for_hint_check():
    verdict = geo.assess(_result(), "Bangalore", _expected(),
                         area=(18.5, 73.8),
                         source_type="DIRECTORY")
    assert verdict["outcome"] == "rejected"
    assert verdict["reason"] == "state_mismatch"


def test_assess_low_confidence_is_approximate():
    verdict = geo.assess(
        _result(rank={"confidence": 0.2, "match_type": "full_match"}),
        "Maharashtra", _expected(), area=(18.5, 73.8))
    assert verdict["outcome"] == "approximate"
    assert verdict["reason"] == "low_confidence"


def test_assess_far_area_is_rejected():
    verdict = geo.assess(
        _result(), "Maharashtra", _expected(), area=(19.5, 73.8))
    assert verdict["outcome"] == "rejected"
    assert verdict["reason"] == "area_mismatch"


def test_assess_trusted_registry_without_result_is_approximate():
    verdict = geo.assess(None, "Maharashtra", _expected(),
                         source_type="REGISTRY")
    assert verdict["outcome"] == "approximate"
    assert verdict["reason"] == "trusted_source"


def test_best_candidate_prefers_verified_then_distance():
    near = geo.assess(_result(), "Maharashtra", _expected(), area=(18.5, 73.8))
    far = geo.assess(_result(lat=18.51), "Maharashtra", _expected(),
                     area=(18.5, 73.8))
    weak = geo.assess(_result(rank={"confidence": 0.2,
                                    "match_type": "full_match"}),
                      "Maharashtra", _expected(), area=(18.5, 73.8))
    assert geo.best_candidate([weak, far, near]) is near


def test_lookup_queries_order_and_contents():
    queries = geo.lookup_queries(_expected(
        address_line1="10 MG Road", address_line2="Raheja Centre"))
    kinds = [kind for kind, _ in queries]
    assert kinds[:2] == ["full", "street_postcode"]
    assert "landmark_city" in kinds
    assert kinds[-1] == "area_city"
    assert "Pune" in queries[0][1]
    assert "411001" in queries[1][1] and "Pune" not in queries[1][1]
    assert "Pune" in queries[-1][1]
    landmark = dict(queries)["landmark_city"]
    assert "Raheja Centre" in landmark and "Pune" in landmark


def test_geocode_many_requests_five_results(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"lat": 1.0}, {"lat": 2.0}]}

    def fake_get(url, params=None, timeout=None):
        captured.update(params)
        return Response()

    monkeypatch.setattr(geo.requests, "get", fake_get)
    results = geo.geocode_many("10 MG Road", api_key="key", limit=5)
    assert len(results) == 2
    assert captured["limit"] == 5
    assert captured["filter"] == "countrycode:in"


def test_country_filter_disabled_for_international_contact(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    def fake_get(url, params=None, timeout=None):
        captured.update(params)
        return Response()

    monkeypatch.setattr(geo.requests, "get", fake_get)
    geo.geocode_many("Dubai address", api_key="key", country_filter=None)
    assert "filter" not in captured
    assert geo.country_filter_for("Dubai") == "ae"
    assert geo.country_filter_for("Kuwait ::: Kuwait") == "kw"
    assert geo.country_filter_for("Manipur ::: Idaho") == "in"
    assert geo.country_filter_for("::: Idaho :::", "Manipur State Power") == "in"
    assert geo.country_filter_for("West Bengal") == "in"
    assert geo.country_name_for("ae") == "United Arab Emirates"


def test_international_country_check_uses_expected_code():
    result = _result(country_code="ae", country="United Arab Emirates")
    checks = geo.check_all(result, "Dubai", _expected(), country_code="ae")
    assert all(item["passed"] for item in checks
               if item["name"] == "country")
    checks_in = geo.check_all(result, "Dubai", _expected(), country_code="in")
    country = [item for item in checks_in if item["name"] == "country"][0]
    assert country["passed"] is False


def test_result_precision_uses_geocoder_result_not_extracted_street():
    assert geo.result_precision(_result(result_type="building")) == "STREET"
    assert geo.result_precision(_result(result_type="suburb", street=None,
                                         housenumber=None)) == "AREA"
    assert geo.result_precision(_result(result_type="city", street=None,
                                         housenumber=None)) == "CITY"


def test_best_candidate_prefers_street_pin_over_closer_city_centroid():
    street = geo.assess(_result(lat=18.51), "Maharashtra", _expected(),
                        area=(18.5, 73.8))
    city = geo.assess(_result(result_type="city", street=None,
                              housenumber=None, lat=18.5, lon=73.8),
                      "Maharashtra", _expected(), area=(18.5, 73.8))
    assert city["pin_precision"] == "CITY"
    assert street["pin_precision"] == "STREET"
    assert geo.best_candidate([city, street]) is street


def test_invalid_geocoder_pin_is_rejected():
    verdict = geo.assess(_result(postcode="996544"), "Maharashtra",
                         _expected(postal_code="996544"), source_type="OWN_SITE")
    assert verdict["outcome"] == "rejected"
    assert verdict["reason"] == "invalid_postcode"


def test_lookup_queries_normalizes_spaced_pin():
    queries = geo.lookup_queries(_expected(postal_code="700 160"))
    assert "700160" in queries[0][1]
    assert "700160" in queries[1][1]


def test_same_city_allows_known_metro_suburbs():
    assert geo.same_city("New Town", "Kolkata")
    assert geo.same_city("Madhapur", "Hyderabad")
    assert not geo.same_city("Howrah", "Kolkata")


def test_street_match_survives_a_wrong_pin_centroid_up_to_limit():
    # Alpha Tech: Mani Casadona is ~33 km from the GeoNames 700160 centroid,
    # but the postcode/city/street text all agree.
    result = _result(city="New Town", state="West Bengal", state_code="WB",
                     postcode="700160", street="Ecospace Seating Area Path2",
                     housenumber="3WS3A", lat=22.5853, lon=88.4864,
                     rank={"confidence": 1.0, "match_type": "full_match"})
    expected = _expected(city="Kolkata", state="West Bengal",
                         postal_code="700160",
                         address_line1="Mani Casadona, Action Area II-F")
    verdict = geo.assess(result, "West Bengal", expected,
                         area=(22.4656, 88.7803), source_type="OWN_SITE")
    assert verdict["outcome"] == "approximate"
    assert verdict["pin_precision"] == "STREET"


def test_city_level_result_far_from_area_is_rejected():
    result = _result(result_type="city", street=None, housenumber=None,
                     city="Kolkata", postcode="700160",
                     lat=22.57, lon=88.36)
    expected = _expected(city="Kolkata", postal_code="700160")
    verdict = geo.assess(result, "West Bengal", expected,
                         area=(22.4656, 88.7803), source_type="OWN_SITE")
    assert verdict["outcome"] == "rejected"
    assert verdict["reason"] == "area_mismatch"
