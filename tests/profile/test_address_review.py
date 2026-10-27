"""Tests for the approximate-location review list."""
from scripts import address_review


def test_format_row_includes_address_and_source():
    row = {
        "company": "Fab Tech India Impex",
        "address_line1": "40 Strand Road",
        "address_line2": "Model House",
        "city": "Kolkata",
        "state": "West Bengal",
        "postal_code": "700001",
        "country": "India",
        "source_type": "REGISTRY",
        "found_at": "2026-10-26",
    }
    line = address_review.format_row(1, row)
    assert "Fab Tech India Impex" in line
    assert "40 Strand Road, Model House, Kolkata" in line
    assert "REGISTRY" in line
