"""Behaviour tests for the combined worker's per-tag paths (no network/DB)."""
from app.profile.workers import combined


def test_wa_number_takes_first_usable_digits():
    assert combined._wa_number(["98765-43210", "111"]) == "9876543210"
    assert combined._wa_number([]) == ""
    assert combined._wa_number([None, "abc"]) == ""


def test_whatsapp_builds_wa_link_and_appends_line(monkeypatch):
    written = []
    monkeypatch.setattr(combined.db, "append_profile_line",
                        lambda cid, label, url: written.append((cid, label, url)))

    result = combined.run({
        "contactId": "c1", "tag": "whatsapp",
        "snapshot": {"mobileNumbers": ["9876543210"]},
    })
    assert result["whatsapp_url"] == "https://wa.me/9876543210"
    assert written == [("c1", "whatsapp", "https://wa.me/9876543210")]


def test_whatsapp_without_number_is_a_noop(monkeypatch):
    monkeypatch.setattr(combined.db, "append_profile_line",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no write")))
    result = combined.run({"contactId": "c1", "tag": "whatsapp", "snapshot": {}})
    assert result["whatsapp_url"] == ""


def test_platform_keeps_name_verified_url(monkeypatch):
    merged = []
    monkeypatch.setattr(combined.ai, "find_platform_profile",
                        lambda name, company, platform, worker="": "https://twitter.com/satyanadella")
    monkeypatch.setattr(combined.db, "merge_enrichment",
                        lambda cid, fields: merged.append(fields))
    monkeypatch.setattr(combined.db, "append_profile_line", lambda *a: None)

    result = combined.run({
        "contactId": "c1", "tag": "twitter",
        "snapshot": {"fullName": "Satya Nadella", "company": "Microsoft"},
    })
    assert result["twitter_url"] == "https://twitter.com/satyanadella"
    assert merged == [{"twitter_url": "https://twitter.com/satyanadella"}]


def test_platform_drops_unverified_url(monkeypatch):
    merged = []
    monkeypatch.setattr(combined.ai, "find_platform_profile",
                        lambda name, company, platform, worker="": "https://twitter.com/microsoft")
    monkeypatch.setattr(combined.db, "merge_enrichment",
                        lambda cid, fields: merged.append(fields))
    monkeypatch.setattr(combined.db, "append_profile_line", lambda *a: None)

    result = combined.run({
        "contactId": "c1", "tag": "twitter",
        "snapshot": {"fullName": "Satya Nadella", "company": "Microsoft"},
    })
    assert result["twitter_url"] == ""
    assert merged == [{}]


def test_research_maps_ai_fields_to_enrichment_columns(monkeypatch):
    data = {
        "linkedin_url": "https://linkedin.com/in/satya-nadella",
        "linkedin_photo_url": "",
        "summary": "Short bio.",
        "linkedin_position": "Current Role: CEO\n• Career: Microsoft",
        "other_web_profiles": "Twitter: https://twitter.com/satyanadella",
        "company_details": "Microsoft details.",
        "enrich_sources": "https://microsoft.com",
        "official_site": "https://microsoft.com",
        "company_core_business": "Cloud.",
        "location": "Redmond, USA",
    }
    merged = {}
    statuses = []
    titles = []
    monkeypatch.setattr(combined.ai, "enrich_person",
                        lambda *a, **k: (dict(data), True))
    monkeypatch.setattr(combined.common, "resolve_photo", lambda *a, **k: "")
    monkeypatch.setattr(combined.db, "merge_enrichment",
                        lambda cid, fields: merged.update(fields))
    monkeypatch.setattr(combined.db, "set_status", lambda cid, s: statuses.append(s))
    monkeypatch.setattr(combined.db, "set_contact_job_title",
                        lambda cid, t: titles.append(t))

    result = combined.run({
        "contactId": "c1", "tag": "research",
        "snapshot": {"fullName": "Satya Nadella", "jobTitle": "", "company": "Microsoft"},
    })
    assert result["ok"] is True
    assert merged["linkedin_url"] == "https://linkedin.com/in/satya-nadella"
    assert merged["summary"] == "Short bio."
    assert merged["career_background"].startswith("\n• ") or "Current Role" in merged["career_background"]
    assert merged["twitter_url"] == "https://twitter.com/satyanadella"
    assert merged["official_site"] == "https://microsoft.com"
    assert statuses == ["done"]
    assert titles == ["CEO"]


def test_research_without_name_or_company_is_rejected():
    result = combined.run({"contactId": "c1", "tag": "research", "snapshot": {}})
    assert result["ok"] is False
