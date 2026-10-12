"""Shared validation helpers for the card-desk workers.

Profile discovery is done by AI (OpenAI web_search); this module provides the
EXACT-match verification on top so a wrong profile is never stored: a
social/LinkedIn URL only counts when its slug/handle actually contains part of
the person's name, and a photo URL is only stored when it really serves an
image. Also photo resolution and job-title cleaning.
"""
import hashlib
import html
import logging
import re
import urllib.parse

import requests

from . import config

log = logging.getLogger("card-desk.common")


def normalize_url(url):
    s = str(url or "").strip()
    if not s:
        return ""
    return s if s.startswith("http") else "https://" + s


# ── Exact-match helpers ────────────────────────────────────────────────────
def _name_tokens(name):
    toks = re.findall(r"[a-z]+", str(name or "").lower())
    return [t for t in toks if len(t) >= 3]


def _li_slug(url):
    m = re.search(r"linkedin\.com/in/([^/?#]+)", str(url or ""), re.I)
    return urllib.parse.unquote(m.group(1)).lower() if m else ""


def li_score(url, name):
    """How many of the person's name tokens appear in the linkedin.com/in/
    slug. -1 = not a profile URL at all; 0 = profile but name doesn't match."""
    slug = _li_slug(url)
    if not slug:
        return -1
    return sum(1 for t in _name_tokens(name) if t in slug)


def path_score(url, name):
    """Name-token match for non-LinkedIn profile URLs (facebook/x/instagram)."""
    try:
        path = urllib.parse.unquote(urllib.parse.urlparse(str(url)).path).lower()
    except Exception:
        return 0
    return sum(1 for t in _name_tokens(name) if t in path.replace("-", "").replace(".", "")
               or t in path)


# ── Photo resolution: only store a URL that ACTUALLY serves an image ───────
def photo_ok(url):
    try:
        r = requests.get(url, timeout=config.SCRAPE_TIMEOUT + 2,
                         headers={"User-Agent": config.USER_AGENT})
        return (r.status_code == 200
                and r.headers.get("content-type", "").startswith("image")
                and len(r.content) > 500)
    except Exception:
        return False


_LI_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def linkedin_og_image(slug):
    """Fetch the LinkedIn public profile page and return the profile photo URL
    from its og:image meta (media.licdn.com/dms/image/...). Works only for
    profiles LinkedIn serves to anonymous visitors; login-restricted profiles
    return HTTP 999 and yield '' — never raises."""
    if not slug:
        return ""
    try:
        r = requests.get(
            "https://www.linkedin.com/in/" + slug, timeout=config.SCRAPE_TIMEOUT + 4,
            headers={"User-Agent": _LI_BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
        if r.status_code != 200:
            return ""
        needle = "og:image"
        i = r.text.find(needle)
        if i < 0:
            return ""
        c = r.text.find("content=", i)
        if c < 0:
            return ""
        s = r.text.find('"', c) + 1
        e = r.text.find('"', s)
        url = r.text[s:e] if 0 < s < e else ""
        url = html.unescape(url)  # og:image escapes & as &amp; — restores the signature
        return url if url.startswith("http") else ""
    except Exception:
        return ""


def resolve_photo(linkedin_url, emails, twitter_url, current=""):
    """Try every photo source in order and return the first URL that really
    returns an image: existing → LinkedIn og:image → LinkedIn avatar (unavatar)
    → email avatar → gravatar → X/Twitter avatar. '' when nothing works
    (frontend then shows initials)."""
    cands = []
    if current and current.startswith("http"):
        cands.append(current)
    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9\-_%\.]+)",
                  str(linkedin_url or ""), re.I)
    if m:
        og = linkedin_og_image(m.group(1))
        if og:
            cands.append(og)
        cands.append("https://unavatar.io/linkedin/" + m.group(1)
                     + "?fallback=false")
    em = ""
    for part in re.split(r"[,;\s]+", str(emails or "")):
        if "@" in part:
            em = part.strip().lower()
            break
    if em:
        cands.append("https://unavatar.io/" + urllib.parse.quote(em)
                     + "?fallback=false")
        cands.append("https://www.gravatar.com/avatar/"
                     + hashlib.md5(em.encode()).hexdigest() + "?d=404&s=256")
    tm = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,})",
                   str(twitter_url or ""))
    if tm:
        cands.append("https://unavatar.io/x/" + tm.group(1) + "?fallback=false")
    for u in cands:
        if photo_ok(u):
            return u
    return ""


# ── Title cleaning ─────────────────────────────────────────────────────────
_TITLE_BAD = ("linkedin", "profile", "shows", "employment", "associated",
              "visible", "publicly", "exact title", "not publicly", "unknown",
              "n/a", "per ", "http", "search", "could not", "no public")


def bad_title(s):
    """A real title is short. Anything long or containing research-speak is a
    sentence the model wrote, not a job title."""
    s = str(s or "").strip()
    if not s:
        return False
    low = s.lower()
    return len(s) > 55 or len(s.split()) > 8 or any(b in low for b in _TITLE_BAD)


def clean_role(raw):
    """Turn 'Data Analyst at Laser Power (per LinkedIn). ([theorg...])' into
    'Data Analyst'. Returns '' when there's no clean title to keep."""
    r = str(raw or "").strip()
    if not r:
        return ""
    r = re.sub(r"\(\[.*$", "", r)                       # drop ([source])
    r = re.sub(r"\(per\b.*$", "", r, flags=re.I)         # drop (per ...)
    r = re.sub(r"\s*\(.*$", "", r)                       # drop trailing paren
    r = re.split(r"\s+\bat\b\s+", r, 1)[0]               # cut before "at Company"
    r = r.split(". ")[0].split(";")[0].strip(" .,-–—–—")
    return "" if bad_title(r) else r[:80]


# ── Personal-profile filtering ─────────────────────────────────────────────
_PERSONAL_PLATFORMS = ("facebook", "twitter", "x", "instagram")


def keep_personal_only(text, name):
    """Keep every line as-is EXCEPT Facebook/Twitter/X/Instagram lines, which
    are only kept when the URL's path actually matches the person's name.
    A company's own social page is never the person's profile."""
    out = []
    for line in str(text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z ]+):\s*(https?://\S+)", line)
        if m and m.group(1).strip().lower() in _PERSONAL_PLATFORMS:
            if path_score(m.group(2), name) < 1:
                continue  # company/page URL, not this person — drop it
        out.append(line)
    return "\n".join(out)
