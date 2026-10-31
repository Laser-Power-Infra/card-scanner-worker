"""Retrieval orchestration tests: no network, no database."""
from app.profile.workers import address


def _page(url, text, title=""):
    return {"url": url, "title": title or url, "text": text, "links": []}


def _install(monkeypatch, fetch, search=None, sitemap=None):
    monkeypatch.setattr(address, "_fetch_page", fetch)
    monkeypatch.setattr(address, "_sitemap_links",
                        sitemap if sitemap is not None else (lambda root: []))
    monkeypatch.setattr(
        address.search_mod, "search",
        search if search is not None else
        (lambda *a, **k: (_ for _ in ()).throw(AssertionError("no search"))))


def test_site_page_with_address_skips_search(monkeypatch):
    def fetch(url):
        return _page(url, "Registered Office 17 Ezra Street Kolkata 700001 "
                          "West Bengal India " * 4)

    _install(monkeypatch, fetch)
    pages = address._retrieve("Acme", "West Bengal", "https://acme.example", "")
    assert pages
    assert all("acme.example" in page["url"] for page in pages)


def test_wider_search_runs_unscoped_when_no_site_address(monkeypatch):
    calls = []

    def fetch(url):
        return _page(url, "Welcome. Head Office. About Us. Contact Us. "
                          "Products and services for our customers. " * 5)

    def search(query, domains=None, lean=False):
        calls.append(domains)
        return [_page("https://directory.example/acme",
                      "Acme Registered Office 5 Park Street Kolkata 700016 "
                      "West Bengal India " * 5)]

    _install(monkeypatch, fetch, search=search)
    pages = address._retrieve("Acme", "West Bengal", "https://acme.example", "")
    assert calls == [None]
    assert any("directory.example" in page["url"] for page in pages)


def test_scoped_search_is_fallback_when_unscoped_errors(monkeypatch):
    calls = []

    def fetch(url):
        return _page(url, "Welcome. Head Office. About Us. " * 8)

    def search(query, domains=None, lean=False):
        calls.append(domains)
        if domains is None:
            raise RuntimeError("unscoped unavailable")
        return [_page("https://acme.example/contact",
                      "Registered Office 5 Park Street Kolkata 700016 "
                      "West Bengal India " * 5)]

    _install(monkeypatch, fetch, search=search)
    pages = address._retrieve("Acme", "West Bengal", "https://acme.example", "")
    assert calls == [None, ["https://acme.example"]]
    assert any(page["url"].endswith("/contact") for page in pages)


def test_discovered_contact_link_is_followed(monkeypatch):
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url.rstrip("/") == "https://acme.example":
            return {
                "url": "https://acme.example/",
                "title": "/",
                "text": "Welcome to Acme. Products and services.",
                "links": ["https://acme.example/Contact-Us.html"],
            }
        if url.endswith("Contact-Us.html"):
            return _page(url,
                         "Registered Office 17 Ezra Street Kolkata 700001 "
                         "West Bengal India. Corporate office details. "
                         "Branch office details are listed on this page.")
        return None

    _install(monkeypatch, fetch)
    pages = address._retrieve("Acme", "West Bengal", "https://acme.example", "")
    assert "https://acme.example/Contact-Us.html" in fetched
    assert any(page["url"].endswith("Contact-Us.html") for page in pages)


def test_dead_search_result_is_dropped(monkeypatch):
    def fetch(url):
        return _page(url, "Welcome. Head Office. About Us. " * 8)

    def search(query, domains=None, lean=False):
        return [
            _page("https://dead.example/acme",
                  "Account suspended. This website is suspended. " * 10),
            _page("https://good.example/acme",
                  "Acme Registered Office 5 Park Street Kolkata 700016 "
                  "West Bengal India " * 5),
        ]

    _install(monkeypatch, fetch, search=search)
    pages = address._retrieve("Acme", "West Bengal", "https://acme.example", "")
    assert not any("dead.example" in page["url"] for page in pages)
    assert any("good.example" in page["url"] for page in pages)


def test_company_mismatch_keeps_low_trust_pages(monkeypatch):
    def fetch(url):
        return _page("https://directory.example/other",
                     "Other Company. 10 MG Road Kolkata 700001 West Bengal "
                     "India. Products and services. " * 4,
                     title="Other Company")

    _install(monkeypatch, fetch)
    notes = []
    pages = address._retrieve("Acme", "West Bengal", "https://acme.example", "",
                              notes=notes)
    assert pages
    assert all(page.get("company_trust") == "low" for page in pages)
    assert any(note.startswith("company_filter_fallback") for note in notes)
