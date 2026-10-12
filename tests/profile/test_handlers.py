"""Dispatch/wiring tests for the profile-collection handlers."""
from app.profile import handlers


CONTACT = {
    "fullName": "Satya Nadella",
    "company": "Microsoft",
    "jobTitle": "CEO",
    "emails": ["satya@microsoft.com"],
    "website": "microsoft.com",
    "linkedin": "https://linkedin.com/in/satya-nadella",
    "otherSocials": ["https://twitter.com/satyanadella"],
    "mobileNumbers": ["9876543210"],
    "telephoneNumbers": [],
}


def test_snapshot_from_contact_selects_expected_fields():
    snap = handlers.snapshot_from_contact(CONTACT)
    assert snap["fullName"] == "Satya Nadella"
    assert snap["emails"] == ["satya@microsoft.com"]
    assert snap["mobileNumbers"] == ["9876543210"]
    # Unmapped fields are dropped.
    assert "rawNotes" not in snap


def test_snapshot_handles_empty():
    snap = handlers.snapshot_from_contact(None)
    assert snap["fullName"] is None
    assert snap["emails"] == []


def test_task_from_payload_maps_contact_id_tag_snapshot():
    task = handlers._task_from_payload(
        {"contactId": "c1", "tag": "research", "contact": CONTACT})
    assert task["contactId"] == "c1"
    assert task["tag"] == "research"
    assert task["snapshot"]["company"] == "Microsoft"


def test_every_tag_is_dispatched_to_the_combined_worker():
    for tag in handlers.TAGS:
        calls = []

        def fake_run(task):
            calls.append(task)
            return {"ok": True}

        original = handlers.combined.run
        handlers.combined.run = fake_run
        try:
            result = handlers.dispatch(tag, {"contactId": "c1", "tag": tag,
                                             "contact": CONTACT})
        finally:
            handlers.combined.run = original
        assert result == {"ok": True}
        assert len(calls) == 1
        assert calls[0]["tag"] == tag


def test_dispatch_returns_none_for_unknown_tag():
    assert handlers.dispatch("nope", {"contactId": "c1"}) is None


def test_process_payload_outcomes(monkeypatch):
    # No contact id -> skipped.
    assert handlers.process_payload({"tag": "research"})[0] == "skipped"

    # Unknown tag -> skipped.
    outcome, reason = handlers.process_payload({"contactId": "c1", "tag": "nope"})
    assert outcome == "skipped"

    # Handler raises -> error.
    def boom(tag, payload):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(handlers, "dispatch", boom)
    outcome, value = handlers.process_payload({"contactId": "c1", "tag": "research"})
    assert outcome == "error"
    assert "kaboom" in value

    # Happy path -> ok.
    monkeypatch.setattr(handlers, "dispatch", lambda tag, payload: {"ok": True})
    outcome, value = handlers.process_payload({"contactId": "c1", "tag": "research"})
    assert outcome == "ok"
    assert value == {"ok": True}
