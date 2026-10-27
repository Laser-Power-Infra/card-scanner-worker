"""Offline PIN lookup tests."""
from app.profile import pin_codes


def test_valid_pin_returns_state_district_city_and_centroid():
    info = pin_codes.lookup("500081")
    assert info["valid"] is True
    assert info["state"] == "Telangana"
    assert info["city"] == "Hyderabad"
    assert info["lat"] is not None and info["lon"] is not None


def test_spaced_pin_is_normalized():
    assert pin_codes.normalize_pin("700 160") == "700160"
    assert pin_codes.lookup("700-160")["state"] == "West Bengal"


def test_invalid_pin_is_rejected():
    assert pin_codes.lookup("996544")["valid"] is False
    assert pin_codes.lookup("000000")["valid"] is False
    assert pin_codes.lookup("12345")["valid"] is False


def test_vijayawada_pin_maps_to_andhra():
    info = pin_codes.lookup("520004")
    assert info["valid"] is True
    assert info["state"] == "Andhra Pradesh"
    assert info["city"] == "Vijayawada"
