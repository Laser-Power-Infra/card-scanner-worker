"""Pure-function tests for the profile worker's shared validation helpers."""
from app.profile import common


def test_normalize_url_adds_scheme():
    assert common.normalize_url("linkedin.com/in/x") == "https://linkedin.com/in/x"
    assert common.normalize_url("https://x.com") == "https://x.com"
    assert common.normalize_url("") == ""
    assert common.normalize_url(None) == ""


def test_li_score_matches_name_tokens():
    url = "https://www.linkedin.com/in/satya-nadella/"
    assert common.li_score(url, "Satya Nadella") == 2
    assert common.li_score(url, "Bill Gates") == 0
    # Not a profile URL at all.
    assert common.li_score("https://linkedin.com/company/foo", "Satya Nadella") == -1


def test_path_score_matches_personal_handles():
    assert common.path_score("https://facebook.com/satyanadella", "Satya Nadella") == 2
    assert common.path_score("https://facebook.com/microsoft", "Satya Nadella") == 0


def test_clean_role_trims_researcher_noise():
    assert common.clean_role("Data Analyst at Laser Power (per LinkedIn)") == "Data Analyst"
    assert common.clean_role("Engineer; and more") == "Engineer"
    assert common.clean_role("") == ""
    # Sentence-like output is rejected.
    assert common.clean_role(
        "The exact title is not publicly available from search results") == ""


def test_bad_title_flags_long_or_research_speak():
    assert common.bad_title("Data Analyst") is False
    assert common.bad_title("") is False
    assert common.bad_title("x" * 60) is True
    assert common.bad_title("see the linkedin profile") is True


def test_keep_personal_only_drops_company_social_lines():
    text = "\n".join([
        "Crunchbase: https://crunchbase.com/person/satya",
        "Facebook: https://facebook.com/microsoft",
        "Twitter: https://twitter.com/satyanadella",
    ])
    kept = common.keep_personal_only(text, "Satya Nadella")
    assert "Crunchbase" in kept
    assert "twitter.com/satyanadella" in kept
    assert "facebook.com/microsoft" not in kept
