"""Tests for the Phase 1 console attempt records."""
import logging

from app.profile import address_attempt


def _start():
    return address_attempt.start("c1", "Acme", "West Bengal")


def test_finish_emits_readable_block(caplog):
    caplog.set_level(logging.INFO, logger="card-desk.address-attempt")
    attempt = _start()
    address_attempt.finish(
        attempt, "FAIL", "postcode_mismatch",
        pages=[address_attempt.page("https://acme.example/contact",
                                    "Registered office 12 Park Street\nKolkata")],
        extracted={"address_line1": "12 Park Street", "city": "Kolkata",
                   "state": "West Bengal", "postal_code": "700022",
                   "country": "India"},
        map_result={"formatted": "12 Park St, Kolkata 700022", "lat": 22.5,
                    "lon": 88.3, "resultType": "building",
                    "matchType": "full_match", "confidence": 0.95,
                    "placeId": "pid"},
        checks=[
            address_attempt.check("country", True, "in"),
            address_attempt.check("postcode", False, "700022 != 700001",
                                  "postcode_mismatch"),
        ],
    )

    text = caplog.text
    assert "ADDRESS_ATTEMPT" in text
    assert "contact c1, Acme" in text
    assert "outcome   : FAIL" in text
    assert "reason    : postcode_mismatch" in text
    assert "https://acme.example/contact | Registered office 12 Park Street Kolkata" in text
    assert "line1=12 Park Street" in text
    assert "lat=22.5 lon=88.3" in text
    assert "[PASS] country: in" in text
    assert "[FAIL] postcode: 700022 != 700001" in text


def test_finish_is_idempotent(caplog):
    caplog.set_level(logging.INFO, logger="card-desk.address-attempt")
    attempt = _start()
    address_attempt.finish(attempt, "PASS", "verified")
    address_attempt.finish(attempt, "FAIL", "duplicate")
    assert caplog.text.count("ADDRESS_ATTEMPT") == 1
    assert "reason    : verified" in caplog.text


def test_emitter_can_be_disabled(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="card-desk.address-attempt")
    monkeypatch.setenv("ADDRESS_ATTEMPT_LOG", "0")
    address_attempt.finish(_start(), "FAIL", "no_pages")
    assert "ADDRESS_ATTEMPT" not in caplog.text


def test_notes_and_missing_sections_render():
    attempt = _start()
    address_attempt.note(attempt, "scoped search failed")
    address_attempt.finish(attempt, "FAIL", "no_pages")
    block = address_attempt.format_attempt(attempt)
    assert "pages     : 0" in block
    assert "extracted : -" in block
    assert "map       : -" in block
    assert "checks    : 0 failed of 0" in block
    assert "notes     : scoped search failed" in block


def test_script_parses_blocks():
    from scripts import address_attempts

    first = _start()
    address_attempt.finish(first, "FAIL", "postcode_mismatch", checks=[
        address_attempt.check("postcode", False, "x", "postcode_mismatch"),
        address_attempt.check("country", True, "in"),
    ])
    second = _start()
    address_attempt.finish(second, "PASS", "verified")

    records = address_attempts.parse(
        address_attempt.format_attempt(first).splitlines()
        + address_attempt.format_attempt(second).splitlines())
    assert [r["reason"] for r in records] == ["postcode_mismatch", "verified"]
    assert [r["outcome"] for r in records] == ["FAIL", "PASS"]
    assert records[0]["failed"] == ["postcode"]


def test_script_joins_powershell_wrapped_header():
    from scripts import address_attempts

    lines = [
        "2026-10-26 15:38:58 card-desk.address-attempt INFO ADDRESS_ATTEMPT abc123",
        "(contact c1, ACME LIMITED)",
        "  outcome   : FAIL",
        "  reason    : low_confidence",
        "    [FAIL] confidence: confidence=0.4",
        "  notes     : wrapped   continuation",
    ]
    records = address_attempts.parse(lines)
    assert len(records) == 1
    assert records[0]["id"] == "abc123"
    assert records[0]["contact"] == "c1"
    assert records[0]["company"] == "ACME LIMITED"
    assert records[0]["reason"] == "low_confidence"
    assert records[0]["failed"] == ["confidence"]
