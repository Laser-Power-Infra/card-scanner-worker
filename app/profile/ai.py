"""OpenAI integration for the 'research' worker.

Port of the legacy card-desk ai.py: the person/company research runs through
OpenAI's Responses API with the built-in `web_search` tool. The search happens
on OpenAI's servers (not our IP), so profile URLs come back reliably even when
free HTML search engines throttle our network.

Output fields (card-desk shape) are mapped to the enrichment table by the
research worker:
    linkedin_url, linkedin_photo_url, summary, linkedin_position,
    other_web_profiles, company_details, official_site, company_core_business,
    location, enrich_sources

Cost controls (set in .env): ENRICH_MODEL (default gpt-6-luna),
ENRICH_EFFORT (low), SEARCH_CONTEXT (medium).
"""
import json
import logging
import re
import time

import requests

from . import config

log = logging.getLogger("card-desk.ai")

RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"

# Structured-output JSON schemas (non-strict: every field optional, model omits
# what it cannot find). parse_and_validate treats missing keys as empty strings.
_ENRICH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "linkedin_url": {
            "type": "string",
            "description": "Exact linkedin.com/in/<slug> profile URL, or empty string.",
        },
        "linkedin_photo_url": {
            "type": "string",
            "description": "MANDATORY: the exact media.licdn.com/dms/image/... CDN profile photo URL "
                         "from the LinkedIn profile photo (og:image), or empty string only if the "
                         "profile has no public photo.",
        },
        "summary": {
            "type": "string",
            "description": "2-3 sentence plain-text bio. No URLs, no links, no citations.",
        },
        "linkedin_position": {
            "type": "string",
            "description": "Bullet-line career profile. Plain text only, no URLs or citations.",
        },
        "other_web_profiles": {
            "type": "string",
            "description": "Labelled 'Platform: url' lines for the person's own personal profiles.",
        },
        "company_details": {
            "type": "string",
            "description": "2-3 sentence plain-text company description. No URLs or citations.",
        },
        "enrich_sources": {
            "type": "string",
            "description": "Comma-separated URLs actually used as sources.",
        },
        "official_site": {
            "type": "string",
            "description": "The company's official https:// website URL, or empty string.",
        },
        "company_core_business": {
            "type": "string",
            "description": "1-2 sentence plain-text core business. No URLs or citations.",
        },
        "location": {
            "type": "string",
            "description": "Company headquarters location, e.g. 'Bath, UK', or empty string.",
        },
    },
    "required": ["linkedin_photo_url"],
}

_PLATFORM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "url": {
            "type": "string",
            "description": "Confirmed personal profile URL, or empty string if not found.",
        },
    },
}


def _output_format(name, schema):
    """OpenAI Responses API structured-output block: ask the model to answer as
    JSON conforming to `schema` (non-strict, all fields optional)."""
    return {
        "text": {
            "format": {
                "type": "json_schema",
                "name": name,
                "strict": False,
                "schema": schema,
            }
        }
    }


def _headers():
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing; add it to .env")
    return {"Authorization": "Bearer " + config.OPENAI_API_KEY,
            "Content-Type": "application/json"}


def strip_company_suffix(name):
    if not name:
        return ""
    s = str(name)
    s = re.sub(r"\s*(Pvt\.?|Private)\s+(Ltd\.?|Limited)\.?$", "", s, flags=re.I)
    s = re.sub(r"\s+(Ltd\.?|Limited|LLC|LLP|Inc\.?|Corp\.?|Corporation|Co\.?)\.?$",
               "", s, flags=re.I)
    return s.rstrip(".,").strip()


def build_enrich_prompt(full_name, designation, company, company_short):
    if company:
        company_line = 'COMPANY: {0} (search also as "{1}")'.format(
            company, company_short or company)
    else:
        company_line = "COMPANY: not provided - research the person by name only."

    return """You are a professional business researcher with a live web search tool.
Research the person and company below using ALL available web sources - not just LinkedIn:
official company website, news articles, business directories, conference bios,
press releases, interviews, and ALL social platforms: Twitter/X, Facebook, Instagram,
and any professional listing.

PERSON: {name}
DESIGNATION: {desig}
{company_line}

TASKS:
1. linkedin_url - Find the exact profile URL matching linkedin.com/in/<slug>.
   STRICT: the slug must clearly correspond to THIS person's name (it normally
   contains their first and/or last name) AND the result snippet must mention
   their company or role. NEVER invent a slug. No search-page URLs. A wrong
   profile is worse than none - if unsure, return "".
2. linkedin_photo_url - MANDATORY: the exact CDN profile photo URL
   (media.licdn.com/dms/image/... or media-exp1/exp2.licdn.com) that the
   LinkedIn profile page shows as its photo (og:image / profile-displayphoto).
   Look inside the LinkedIn result snippet and any image URLs it contains and
   return the direct photo URL. Return "" ONLY when the profile has no public
   photo - never a generic placeholder or a non-photo URL.
3. summary - 2-3 plain-English sentences: who this person is, what the company does,
   and anything notable. Written for a directory card. No bullets here.
4. linkedin_position - Bullet-point PERSON profile from all sources combined.
   Write each bullet on its OWN line (newline-separated):
   • Current Role: ...
   • Career History: (one line per role with company and dates if known)
   • Education: ...
   • Expertise: ...
   • Notable: ...
   • Network: ...
   Write "Not publicly available" for empty bullets.
5. other_web_profiles - Hunt the person's OWN personal SOCIAL profiles - NEVER
   the company's official page. Before including a Facebook/Twitter/Instagram
   link, confirm the profile belongs to THIS person - a page named after the
   company, a brand account, or a page you're only guessing is theirs must be
   left out entirely. List every confirmed line one per line, labelled:
   Facebook: <url> | Twitter: <url> | Instagram: <url> | YouTube: <url> |
   Crunchbase: <url> | News: <url>. Only URLs actually seen in search results.
   If NO personal profiles are found, list instead the best pages about the
   person's COMPANY (official site, about page, news), labelled:
   Company: <url> | News: <url>
6. enrich_sources - URLs you actually used, comma-separated.
7. company_details - 2-3 plain-English sentences describing the company
   (what it does, who it serves, one notable thing). Concise, for a directory
   card. No bullets.
8. official_site - The company's official website URL (https://<domain>). Only
   a real URL seen in search results - never a directory or social page. If you
   cannot confirm the official domain, return "".
9. company_core_business - 1-2 plain-English sentences on the company's CORE
   business (its main product/service and primary customers). Distinct from
   company_details - keep it tight and factual. No bullets.
10. location - The company's headquarters location (city, country) or the
    person's location when the HQ is unknown. Short, e.g. "San Francisco, USA".
    If unknown, return "".

RULES: Every fact must come from live search results - never invent.
NEVER put citation links, [text](url), ([text](url)), "Website: <url>",
"(Source: ...)" or any URL inside the field values (summary, linkedin_position,
company_details, company_core_business, location) - those fields must be plain
text only. URLs belong ONLY in official_site, other_web_profiles and
enrich_sources.
Respond with ONLY this JSON (no markdown fences):
{{"linkedin_url":"", "linkedin_photo_url":"", "summary":"", "linkedin_position":"", "other_web_profiles":"", "enrich_sources":"", "company_details":"", "official_site":"", "company_core_business":"", "location":""}}""".format(
        name=full_name or "Unknown", desig=designation or "Unknown",
        company_line=company_line)


_PLATFORM_LABELS = {
    "linkedin": "LinkedIn",
    "facebook": "Facebook",
    "twitter": "Twitter/X",
    "instagram": "Instagram",
}

_PLATFORM_DOMAINS = {
    "linkedin": "linkedin.com/in/<slug>",
    "facebook": "facebook.com/<handle>",
    "twitter": "twitter.com/<handle> or x.com/<handle>",
    "instagram": "instagram.com/<handle>",
}


def find_platform_profile(full_name, company, platform, worker=""):
    """Focused live web search for ONE platform's profile URL via OpenAI
    Responses + web_search. Returns the candidate URL (or ''). The caller still
    verifies the handle against the person's name (exact-match rule)."""
    caller = worker or platform
    log.info("AI call (%s): platform=%s name=%r company=%r",
             caller, platform, full_name, company)
    t0 = time.monotonic()

    label = _PLATFORM_LABELS.get(platform, platform.capitalize())
    domain = _PLATFORM_DOMAINS.get(platform, "")
    prompt = (
        "You are a professional researcher with a live web search tool.\n"
        "Find the {label} profile URL for this person.\n"
        "PERSON: {name}\n"
        "COMPANY: {company}\n\n"
        "STRICT RULES:\n"
        "- The profile must clearly belong to THIS person: its handle/slug must "
        "contain their name, and the result snippet must match their role/company.\n"
        "- It must be a personal profile ({domain}), never a company/brand page, "
        "never a search-results page, never a share/login page.\n"
        "- NEVER guess or invent a handle. If you cannot confirm the profile, "
        "return an empty string.\n"
        "- A wrong profile is worse than none.\n\n"
        'Respond with ONLY this JSON: {{"url":""}}'
    ).format(label=label, name=full_name or "Unknown",
             company=company or "not provided", domain=domain)

    payload = {
        "model": config.ENRICH_MODEL,
        "input": prompt,
        "reasoning": {"effort": config.ENRICH_EFFORT},
        "tools": [{"type": "web_search",
                   "search_context_size": config.SEARCH_CONTEXT}],
        "tool_choice": "required",
        "max_output_tokens": 400,
        **_output_format("platform_url", _PLATFORM_SCHEMA),
    }
    try:
        resp = requests.post(RESPONSES_ENDPOINT, headers=_headers(),
                             json=payload, timeout=180)
    except requests.RequestException as e:
        log.warning("AI call (%s): platform=%s FAILED (network error: %s in %.1fs)",
                    caller, platform, str(e)[:150], time.monotonic() - t0)
        return ""
    if resp.status_code != 200:
        log.warning("AI call (%s): platform=%s FAILED (%s %s in %.1fs)",
                    caller, platform, resp.status_code,
                    resp.text[:150], time.monotonic() - t0)
        return ""
    try:
        result = resp.json()
    except ValueError:
        log.warning("AI call (%s): platform=%s FAILED (response parse error)",
                    caller, platform)
        return ""

    raw_text = ""
    for item in result.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text" and block.get("text"):
                raw_text += block["text"]

    m = re.search(r'"url"\s*:\s*"([^"]+)"', raw_text)
    if not m:
        log.info("AI call (%s): platform=%s -> NOT FOUND in %.1fs",
                 caller, platform, time.monotonic() - t0)
        return ""
    url = m.group(1).strip()
    url = url if url.startswith("http") else ""
    log.info("AI call (%s): platform=%s -> %s in %.1fs",
             caller, platform, url or "NOT FOUND", time.monotonic() - t0)
    return url


def _fallback(reason):
    return {
        "linkedin_url": "",
        "linkedin_photo_url": "",
        "summary": "",
        "linkedin_position": "Research issue: " + reason,
        "other_web_profiles": "",
        "company_details": "",
        "enrich_sources": "Fallback: " + reason,
        "official_site": "",
        "company_core_business": "",
        "location": "",
    }


def enrich_person(full_name, designation, company, worker="research"):
    """Live web research via OpenAI Responses + web_search.
    Returns (data_dict, api_ok). api_ok=False = transient failure (retry later);
    api_ok=True with empty linkedin_url = genuinely not found."""
    caller = worker or "research"
    log.info("AI call (%s): full research name=%r company=%r",
             caller, full_name, company)
    t0 = time.monotonic()
    company_short = strip_company_suffix(company)
    payload = {
        "model": config.ENRICH_MODEL,
        "input": build_enrich_prompt(full_name, designation, company, company_short),
        "reasoning": {"effort": config.ENRICH_EFFORT},
        "tools": [{"type": "web_search",
                   "search_context_size": config.SEARCH_CONTEXT}],
        "tool_choice": "required",
        "max_output_tokens": 6000,
        **_output_format("enrichment_result", _ENRICH_SCHEMA),
    }
    try:
        resp = requests.post(RESPONSES_ENDPOINT, headers=_headers(),
                             json=payload, timeout=300)
    except requests.RequestException as e:
        log.warning("AI call (%s): full research FAILED (network error: %s in %.1fs)",
                    caller, str(e)[:200], time.monotonic() - t0)
        return _fallback("Network error: " + str(e)[:200]), False

    if resp.status_code != 200:
        log.warning("AI call (%s): full research FAILED (%s %s in %.1fs)",
                    caller, resp.status_code, resp.text[:200],
                    time.monotonic() - t0)
        return _fallback("API error " + str(resp.status_code) + ": "
                         + resp.text[:200]), False

    try:
        result = resp.json()
    except ValueError:
        log.warning("AI call (%s): full research FAILED (response parse error)",
                    caller)
        return _fallback("Response parse error"), False

    raw_text = ""
    for item in result.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text" and block.get("text"):
                raw_text += block["text"]

    if not raw_text:
        log.warning("AI call (%s): full research FAILED (empty model output)",
                    caller)
        return _fallback("Empty model output"), False

    data = parse_and_validate(raw_text)
    log.info("AI call (%s): full research -> api_ok=True linkedin=%s in %.1fs",
             caller, data.get("linkedin_url") or "-", time.monotonic() - t0)
    return data, True


def _to_str(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return ", ".join(str(x).strip() for x in v)
    return str(v).strip()


def is_valid_linkedin_url(url):
    return bool(re.match(
        r"^https?://([a-z]{2,}\.)?linkedin\.com/in/[a-zA-Z0-9\-_%\.]+/?(\?.*)?$",
        url.strip(), re.I))


def is_valid_photo_url(url):
    allowed = [
        "media.licdn.com", "media-exp1.licdn.com", "media-exp2.licdn.com",
        "static.licdn.com", "pbs.twimg.com", "unavatar.io",
        "gravatar.com", "githubusercontent.com", "lh3.googleusercontent.com",
    ]
    try:
        domain = re.sub(r"^https?://", "", url).split("/")[0].lower()
        return any(d in domain for d in allowed)
    except Exception:
        return False


_CITATION_RE = re.compile(r"\s*\(\[[^\]\n]*\]\([^)\n]*\)\)")      # ([text](url))
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)\s]*\)")                 # [text](url)
_INCOMPLETE_RE = re.compile(r"\s*\(\[[^\]]*\]\(\s*[^)\n]*$")       # ([text](https://... truncated tail
_UTM_RE = re.compile(r"[?&]utm_source=[^\s&]+")                    # ?utm_source=openai
_URL_RE = re.compile(r"https?://[^\s)\"']+")                       # bare http(s) url
_WWW_RE = re.compile(r"\bwww\.[a-z0-9.\-]+\.[a-z]{2,}(?:/[^\s)\"']*)?")  # bare www. link
# 'Website: <url>' / 'Source: <url>' style label+url units (incl. when prose follows).
_LABEL_URL_RE = re.compile(
    r"\b(?:official\s+website|website|home\s*page|source|sources|"
    r"reference|link|learn\s+more|read\s+more)\s*:\s*https?://[^\s)\"']+",
    re.I)
# Residual citation markers: (1), [1], (Source: ...), [Reference: ...]
_SRC_TAG_RE = re.compile(
    r"\s*[\(\[]\s*(?:source[s]?|reference|ref|via|citation)\s*[:#]?\s*[^)\]]*[\)\]]",
    re.I)
_ARTIFACT_RE = re.compile(r"\s*[\(\[]\s*\d{1,3}\s*[\)\]]")
_EMPTY_BULLET_RE = re.compile(r"^[ \t]*[•\-–—]?[ \t]*$")


def clean_ai_text(text, keep_urls=False):
    """Strip OpenAI web_search citations and embedded links from a field so it
    holds only the model's actual answer.

    keep_urls=False (prose fields: summary, linkedin_position, company_details,
        company_core_business, location): remove every URL/citation/label ->
        plain text only.
    keep_urls=True (other_web_profiles): strip citations but keep the labelled
        'Label: <url>' lines (that field's job is to hold those URLs)."""
    s = str(text or "")
    s = _CITATION_RE.sub("", s)
    s = _MD_LINK_RE.sub("", s)
    s = _INCOMPLETE_RE.sub("", s)
    s = _UTM_RE.sub("", s)
    if not keep_urls:
        s = _LABEL_URL_RE.sub("", s)
        s = _URL_RE.sub("", s)
        s = _WWW_RE.sub("", s)
        s = _SRC_TAG_RE.sub("", s)
        s = _ARTIFACT_RE.sub("", s)
    lines = []
    for ln in s.split("\n"):
        ln = re.sub(r"[ \t]{2,}", " ", ln).strip()
        if not ln or _EMPTY_BULLET_RE.match(ln):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def parse_and_validate(raw_text):
    cleaned = re.sub(r"```json|```", "", raw_text).strip()
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        return _fallback("No JSON in model response")
    try:
        parsed = json.loads(m.group(0))
    except ValueError:
        return _fallback("JSON parse failed")

    li_url = _to_str(parsed.get("linkedin_url"))
    if li_url and not is_valid_linkedin_url(li_url):
        li_url = ""

    photo = _to_str(parsed.get("linkedin_photo_url"))
    if photo and not is_valid_photo_url(photo):
        photo = ""

    others = _to_str(parsed.get("other_web_profiles"))
    others_clean = "\n".join(
        clean_ai_text(u, keep_urls=True) for u in others.split("\n")
        if u.strip() and re.search(r"https?://\S+\.\S+", u)
    )

    official = _to_str(parsed.get("official_site"))
    if official and not official.startswith("http"):
        official = ""
    if official and not re.match(r"^https?://[^\s]+\.\S+", official):
        official = ""

    return {
        "linkedin_url": li_url,
        "linkedin_photo_url": photo,
        "summary": clean_ai_text(parsed.get("summary")),
        "linkedin_position": clean_ai_text(parsed.get("linkedin_position")),
        "other_web_profiles": others_clean,
        "company_details": clean_ai_text(parsed.get("company_details")),
        "enrich_sources": _to_str(parsed.get("enrich_sources")),
        "official_site": official,
        "company_core_business": clean_ai_text(parsed.get("company_core_business")),
        "location": clean_ai_text(parsed.get("location")),
    }
