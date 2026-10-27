"""Tests for page cleaning, link discovery, and address page ranking."""
from app.profile import page_quality


def _page(url, text, title=""):
    return {"url": url, "title": title, "text": text}


def test_html_to_text_drops_script_and_style():
    html = (
        "<style>.address{content:'999999'}</style>"
        "<script>var address = 'fake 111111';</script>"
        "<p>Registered Office, 17 Ezra Street, Kolkata 700001</p>"
    )
    text = page_quality.html_to_text(html)
    assert "Ezra Street" in text
    assert "999999" not in text
    assert "111111" not in text
    assert "document" not in text


def test_condense_keeps_head_and_tail():
    text = "H" * 20000 + "TAIL-MARKER"
    out = page_quality.condense(text, head=100, tail=20)
    assert out.startswith("H" * 100)
    assert out.endswith("TAIL-MARKER")
    assert "[...]" in out


def test_extract_links_finds_contact_first_and_ignores_external():
    html = (
        '<a href="About-Us.html">About Us</a>'
        '<a href="Contact-Us.html">Contact Us</a>'
        '<a href="https://other.example/contact">External</a>'
    )
    links = page_quality.extract_links(html, "https://totalgroup.in/")
    assert links[0].endswith("Contact-Us.html")
    assert any(link.endswith("About-Us.html") for link in links)
    assert all("other.example" not in link for link in links)


def test_parse_sitemap_and_candidate_urls():
    xml = (
        "<urlset><url><loc>https://acme.example/contact</loc></url>"
        "<url><loc>https://acme.example/blog/post</loc></url></urlset>"
    )
    locs = page_quality.parse_sitemap(xml)
    assert locs == ["https://acme.example/contact",
                    "https://acme.example/blog/post"]
    assert page_quality.is_page_candidate_url(locs[0])
    assert not page_quality.is_page_candidate_url(locs[1])


def test_address_signal_needs_a_pin_or_street_line():
    assert page_quality.has_address_signal(
        "Registered Office 17 Ezra Street Kolkata 700001")
    assert not page_quality.has_address_signal(
        "Head Office About Us Contact Us Locations")


def test_score_prefers_address_page_over_navigation():
    contact = _page("https://acme.example/contact",
                    "Registered Office, 17 Ezra Street, Kolkata 700001. "
                    "Corporate office and branch details are listed here.")
    home = _page("https://acme.example/",
                 "Welcome to Acme. Head Office. About Us. Contact Us. "
                 "Products and services for industrial customers.")
    assert (page_quality.score_page(contact, "Acme", "West Bengal")
            > page_quality.score_page(home, "Acme", "West Bengal"))


def test_select_pages_drops_dead_and_thin_and_caps():
    good = [
        _page("https://acme.example/contact-%d" % index,
              "Registered Office %d Park Street Kolkata 700001 " % index * 10)
        for index in range(7)
    ]
    dead = _page("https://acme.example/dead",
                 "Account suspended. This website is suspended. " * 10)
    thin = _page("https://acme.example/thin", "Contact us")
    selected = page_quality.select_pages(good + [dead, thin], "Acme",
                                         "West Bengal", limit=5)
    assert len(selected) == 5
    assert dead not in selected
    assert thin not in selected


def test_url_key_ignores_www_and_trailing_slash():
    assert (page_quality.url_key("https://www.acme.example/contact/")
            == page_quality.url_key("https://acme.example/contact"))


def test_page_relevance_rejects_similar_company_name():
    as_fabtech = _page(
        "https://tracxn.com/as-fabtech",
        "A.S. FABTECH (INDIA) PRIVATE LIMITED company profile. "
        "Kolkata, West Bengal, India.")
    assert page_quality.company_match(
        as_fabtech, "Fab Tech India Impex", "West Bengal")[0] not in ("high", "medium")


def test_short_name_word_does_not_match_unrelated_host():
    # "air" is a substring of "fairdealdirectory" but not a host label.
    page = _page("https://www.fairdealdirectory.in/contact",
                 "Our office details and phone numbers.")
    assert page_quality.company_match(
        page, "Air India", "Kolkata")[0] not in ("high", "medium")


def test_page_relevance_accepts_exact_company_and_city():
    correct = _page(
        "https://www.figmentglobal.com/fab-tech-india-impex",
        "Fab Tech India Impex GST number 19AAGFF3119B1ZU. "
        "North Twenty Four Parganas, West Bengal, India.")
    assert page_quality.company_match(
        correct, "Fab Tech India Impex", "West Bengal")[0] in ("high", "medium")


def test_page_relevance_accepts_gstin_prefix():
    page = _page("https://example.com/x",
                 "Fab Tech India Impex GSTIN 19AAGFF3119B1ZU")
    assert page_quality.company_match(
        page, "Fab Tech India Impex", "West Bengal")[0] in ("high", "medium")


def test_select_pages_order_is_deterministic():
    pages = [
        _page("https://acme.example/contact-b",
              "Registered Office 10 Park Street Kolkata 700001 " * 5),
        _page("https://acme.example/contact-a",
              "Registered Office 10 Park Street Kolkata 700001 " * 5),
        _page("https://acme.example/contact-c",
              "Registered Office 10 Park Street Kolkata 700001 " * 5),
    ]
    first = [page["url"] for page in
             page_quality.select_pages(pages, "Acme", "West Bengal")]
    second = [page["url"] for page in
              page_quality.select_pages(list(reversed(pages)), "Acme",
                                        "West Bengal")]
    assert first == second


def test_quote_must_be_verbatim_on_page():
    page = "Registered Office at 10 MG Road, Pune 411001."
    assert page_quality.quote_in_page("Registered Office at 10 MG Road", page)
    assert not page_quality.quote_in_page(
        "A sentence that is not on this page", page)


def test_compact_name_matches_alphatech_solutions():
    page = _page("https://alphatechsolutions.in/contact-us",
                 "ALPHATECH SOLUTIONS Unit No 3WS3A, Mani Casadona, "
                 "Newtown, Kolkata 700 160")
    tier, reasons = page_quality.company_match(
        page, "ALPHA TECH SOLUTIONS", "West Bengal")
    assert tier == "high"
    assert reasons


def test_city_state_alias_keeps_plr_page():
    page = _page("https://plrprojects.com/AboutusJV.html",
                 "PLR Projects Private Limited, ASTRA Towers, Kondapur, "
                 "Hitech City, Hyderabad 500084")
    tier, _ = page_quality.company_match(
        page, "PLR Projects Private Limited", "Telangana")
    assert tier == "high"


def test_location_only_page_is_low_trust():
    page = _page("https://directory.example/plr",
                 "Some other listing. Hyderabad, Telangana.")
    tier, reasons = page_quality.company_match(
        page, "PLR Projects Private Limited", "Telangana")
    assert tier == "low"
    assert "location_only" in reasons


def test_company_tokens_drop_card_location_words():
    tokens = page_quality.company_tokens(
        "Public Health Engineering Directorate, Govt. of West Bengal",
        "West Bengal")
    assert tokens == ["public", "health", "engineering"]


def test_domain_match_does_not_use_contact_website_for_directory_pages():
    page = _page("https://directory.example/other",
                 "Other Company. 10 MG Road Kolkata 700001 West Bengal India.")
    tier, _ = page_quality.company_match(
        page, "Acme", "West Bengal", website="https://acme.example")
    assert tier == "low"


def test_has_street_address_accepts_spaced_pin():
    assert page_quality.has_street_address(
        "Unit 3, Mani Casadona, Newtown, Kolkata 700 160")
    assert not page_quality.has_street_address("Head Office, Hyderabad 500081")

