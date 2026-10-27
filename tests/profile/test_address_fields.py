"""Field derivation tests: no guessing, PIN/context fill only."""
from app.profile import address_fields


def test_spaced_pin_and_page_context_fill_alpha_tech_address():
    fields = {
        "address_line1": "Unit No 3WS3A, West Tower, Third Floor, Mani Casadona",
        "address_line2": "Plot No 11F/04, Action Area II-F",
        "city": "", "state": "", "postal_code": "700 160", "country": "",
    }
    page = ("ALPHATECH SOLUTIONS Unit No 3WS3A, Mani Casadona, "
            "Action Area II-F, Newtown, Kolkata - 700 160, India")
    result = address_fields.fill(fields, page_text=page)
    assert result["fields"]["postal_code"] == "700160"
    assert result["fields"]["city"] == "Kolkata"
    assert result["fields"]["state"] == "West Bengal"
    assert result["fields"]["country"] == "India"
    assert address_fields.deliverable(result["fields"]) is True


def test_pin_fills_missing_city_and_state_when_page_has_none():
    fields = {"address_line1": "NCC House, Madhapur",
              "city": "", "state": "", "postal_code": "500081",
              "country": ""}
    result = address_fields.fill(fields)
    assert result["fields"]["city"] == "Hyderabad"
    assert result["fields"]["state"] == "Telangana"
    assert result["fields"]["country"] == "India"


def test_company_location_fills_city_when_pin_is_missing():
    fields = {"address_line1": "12 Park Street", "city": "",
              "state": "", "postal_code": "", "country": ""}
    result = address_fields.fill(fields, company_location="Kolkata, West Bengal")
    assert result["fields"]["city"] == "Kolkata"
    assert result["fields"]["state"] == "West Bengal"
    assert address_fields.deliverable(result["fields"]) is True


def test_invalid_pin_is_flagged_and_dropped():
    result = address_fields.fill({"address_line1": "12 Park Street",
                                  "city": "Kolkata", "postal_code": "996544"})
    assert result["fields"]["postal_code"] == ""
    assert any(conflict.startswith("invalid_pin:") for conflict in result["conflicts"])


def test_pin_state_corrects_model_state_and_keeps_breadcrumb():
    result = address_fields.fill(
        {"address_line1": "32-35-54 Sriven Residency",
         "city": "Vijayawada", "state": "Telangana",
         "postal_code": "520004"})
    assert result["fields"]["state"] == "Andhra Pradesh"
    assert any(item.startswith("state=Andhra Pradesh from pin")
               for item in result["derived"])
    assert any(conflict.startswith("state_pin_corrected:")
               for conflict in result["conflicts"])


def test_street_line_is_still_required():
    result = address_fields.fill({"address_line1": "", "city": "Kolkata",
                                  "postal_code": "700022"})
    assert address_fields.deliverable(result["fields"]) is False


def test_international_postal_code_is_preserved():
    result = address_fields.fill(
        {"address_line1": "1 Sheikh Zayed Road", "city": "Dubai",
         "postal_code": "00000", "country": ""},
        company_location="Dubai", india=False)
    assert result["fields"]["postal_code"] == "00000"
    assert result["fields"]["country"] == "United Arab Emirates"
    assert address_fields.deliverable(result["fields"], india=False) is True
