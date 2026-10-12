"""Pure-function tests for the profile worker's OpenAI output handling."""
import json

from app.profile import ai


def test_strip_company_suffix():
    assert ai.strip_company_suffix("Laser Power Pvt. Ltd.") == "Laser Power"
    assert ai.strip_company_suffix("Acme Corp.") == "Acme"
    assert ai.strip_company_suffix("") == ""


def test_clean_ai_text_strips_links_and_citations():
    raw = "Bio ([source](https://x.com)) here"
    assert ai.clean_ai_text(raw) == "Bio here"
    assert "http" not in ai.clean_ai_text("Website: https://acme.com and more")


def test_clean_ai_text_keep_urls_preserves_labelled_lines():
    raw = "Twitter: https://twitter.com/satyanadella"
    assert ai.clean_ai_text(raw, keep_urls=True) == raw


def test_validators():
    assert ai.is_valid_linkedin_url("https://linkedin.com/in/satya-nadella") is True
    assert ai.is_valid_linkedin_url("https://linkedin.com/company/microsoft") is False
    assert ai.is_valid_photo_url("https://media.licdn.com/dms/image/abc") is True
    assert ai.is_valid_photo_url("https://example.com/photo.jpg") is False


def test_parse_and_validate_coerces_and_filters():
    raw = json.dumps({
        "linkedin_url": "https://linkedin.com/in/satya-nadella",
        "linkedin_photo_url": "https://media.licdn.com/dms/image/abc",
        "summary": "A bio with a link https://example.com",
        "other_web_profiles": "Twitter: https://twitter.com/satyanadella",
        "official_site": "https://microsoft.com",
        "company_core_business": "Cloud and software.",
        "location": "Redmond, USA",
    })
    out = ai.parse_and_validate(raw)
    assert out["linkedin_url"] == "https://linkedin.com/in/satya-nadella"
    assert out["linkedin_photo_url"] == "https://media.licdn.com/dms/image/abc"
    assert "example.com" not in out["summary"]
    assert "twitter.com/satyanadella" in out["other_web_profiles"]
    assert out["official_site"] == "https://microsoft.com"


def test_parse_and_validate_drops_bad_links():
    raw = json.dumps({
        "linkedin_url": "https://linkedin.com/company/x",
        "linkedin_photo_url": "https://notaphoto.example/x.jpg",
        "official_site": "not-a-url",
    })
    out = ai.parse_and_validate(raw)
    assert out["linkedin_url"] == ""
    assert out["linkedin_photo_url"] == ""
    assert out["official_site"] == ""


def test_parse_and_validate_falls_back_on_garbage():
    out = ai.parse_and_validate("not json at all")
    assert out["linkedin_url"] == ""
    assert "No JSON" in out["enrich_sources"]
