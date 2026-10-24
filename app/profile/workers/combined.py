"""Combined worker — the single tag-aware handler for every profile tag.

  research  -> full OpenAI web_search research (everything) + DB save
  linkedin / facebook / twitter / instagram -> one focused AI call for that
              platform + DB save
  whatsapp  -> wa.me link from the mobile number (no AI)

Keeps the exact-match rules: a profile URL is stored only when its slug/handle
contains part of the person's name, and a photo URL only when it serves an image.

Research field mapping -> enrichment table:
  linkedin_url          -> linkedin_url
  linkedin_photo_url    -> linkedin_photo_url + avatar_url
  summary               -> summary
  linkedin_position     -> career_background (+ Current Role -> Contact.jobTitle)
  other_web_profiles    -> other_profiles (+ facebook_url/twitter_url/instagram_url)
  company_details       -> company_details
  enrich_sources        -> sources
  company_core_business -> company_core_business
  official_site         -> official_site
  location              -> location
"""
import logging
import re

from .. import ai, common, db

log = logging.getLogger("card-desk.worker.combined")

_PLATFORM_COLUMNS = {
    "linkedin": "linkedin_url",
    "facebook": "facebook_url",
    "twitter": "twitter_url",
    "instagram": "instagram_url",
}


def _wa_number(numbers):
    for num in numbers or []:
        digits = re.sub(r"[^0-9]", "", str(num or ""))
        if digits:
            return digits
    return ""


def _whatsapp(contact_id, snap):
    number = _wa_number(snap.get("mobileNumbers") or
                        snap.get("telephoneNumbers") or [])
    if not number:
        log.info("WhatsApp for contact %s: no mobile number", contact_id)
        return {"ok": True, "site": "whatsapp", "whatsapp_url": ""}
    url = "https://wa.me/" + number
    db.append_profile_line(contact_id, "whatsapp", url)
    log.info("WhatsApp for contact %s: %s", contact_id, url)
    return {"ok": True, "site": "whatsapp", "whatsapp_url": url}


def _platform(contact_id, snap, tag):
    name = str(snap.get("fullName") or "").strip()
    company = str(snap.get("company") or "").strip()
    column = _PLATFORM_COLUMNS.get(tag)
    if column is None:
        return {"ok": False, "site": tag}

    url = ""
    if name:
        try:
            url = ai.find_platform_profile(
                name, company, tag, worker="workers.combined(%s)" % tag)
        except Exception:
            log.exception("ai find_platform_profile failed (continuing)")
        if tag == "linkedin":
            if common.li_score(url, name) < 1:
                url = ""  # name-verified match only
        elif common.path_score(url, name) < 1:
            url = ""  # name-verified match only — never a random same-name page

    fields = {}
    if url:
        fields[column] = url
    db.merge_enrichment(contact_id, fields)
    if tag != "linkedin":
        db.append_profile_line(contact_id, tag, url)

    log.info("%s for contact %s: %s", tag.capitalize(), contact_id, url or "-")
    return {"ok": True, "site": tag, column: url}


def _research(contact_id, snap):
    name = str(snap.get("fullName") or "").strip()
    desig = str(snap.get("jobTitle") or "").strip()
    company = str(snap.get("company") or "").strip()

    if not name and not company:
        return {"ok": False, "site": "research", "reason": "no name/company"}

    data, api_ok = ai.enrich_person(name, desig, company,
                                    worker="workers.combined(research)")

    # Trust the model's social URLs (no HTTP verification, per design).
    # LinkedIn keeps its exact-name gate to avoid same-name collisions.
    linkedin_url = data.get("linkedin_url", "")
    if common.li_score(linkedin_url, name) < 1:
        linkedin_url = ""
    data["linkedin_url"] = linkedin_url

    # Profile photo: test every source and store only a URL that serves an image.
    photo = data.get("linkedin_photo_url", "")
    try:
        photo = common.resolve_photo(linkedin_url, snap.get("emails", ""),
                                     "", current=photo)
    except Exception:
        log.exception("resolve_photo failed (continuing)")

    # Parse the personal FB/TW/IG URLs out of the AI's lines into their columns.
    plat_urls = {}
    for line in (data.get("other_web_profiles", "") or "").split("\n"):
        m = re.match(r"^([A-Za-z ]+):\s*(https?://\S+)", line.strip())
        if not m:
            continue
        label = m.group(1).strip().lower()
        url = m.group(2).strip()
        if label in ("facebook",):
            plat_urls["facebook_url"] = url
        elif label in ("twitter", "x"):
            plat_urls["twitter_url"] = url
        elif label == "instagram":
            plat_urls["instagram_url"] = url

    # Append a WhatsApp wa.me line to other_profiles when a mobile exists.
    number = _wa_number(snap.get("mobileNumbers") or
                        snap.get("telephoneNumbers") or [])
    if number:
        wa_line = "WhatsApp: https://wa.me/" + number
        others = [l.strip() for l in (data.get("other_web_profiles") or "")
                  .split("\n") if l.strip()]
        if wa_line not in others:
            others.append(wa_line)
        data["other_web_profiles"] = "\n".join(others)

    def _writable(key, value):
        # Never write AI fallback text on a failed call; on success, AI research
        # is authoritative so a non-empty value is written.
        if not value or not api_ok:
            return False
        return True

    fields = {}
    if _writable("linkedin_url", linkedin_url):
        fields["linkedin_url"] = linkedin_url
    if _writable("linkedin_photo_url", photo):
        fields["linkedin_photo_url"] = photo
        fields["avatar_url"] = photo
    for col, val in plat_urls.items():
        if _writable(col, val):
            fields[col] = val
    if _writable("summary", data.get("summary", "")):
        fields["summary"] = data["summary"][:2000]
    career_bg = re.sub(r"[ \t]*•[ \t]*", "\n• ",
                       data.get("linkedin_position", "")).strip("\n")
    if _writable("career_background", career_bg):
        fields["career_background"] = career_bg[:2000]
    if _writable("other_profiles", data.get("other_web_profiles", "")):
        fields["other_profiles"] = data["other_web_profiles"][:900]
    if _writable("company_details", data.get("company_details", "")):
        fields["company_details"] = data["company_details"][:2000]
    if _writable("sources", data.get("enrich_sources", "")):
        fields["sources"] = data["enrich_sources"][:2000]
    if _writable("company_core_business", data.get("company_core_business", "")):
        fields["company_core_business"] = data["company_core_business"][:2000]
    if _writable("official_site", data.get("official_site", "")):
        fields["official_site"] = data["official_site"][:500]
    if _writable("location", data.get("location", "")):
        fields["location"] = data["location"][:200]

    db.merge_enrichment(contact_id, fields)
    if api_ok:
        db.set_status(contact_id, "done")
    else:
        log.warning("Research API failed for contact %s (status left pending)",
                    contact_id)

    # Job-title backfill: only from real AI output, only when no card title.
    try:
        if not desig and api_ok:
            m = re.search(r"Current Role\s*[:\-]\s*(.+)",
                          data.get("linkedin_position", ""), re.I)
            role = common.clean_role(m.group(1)) if m else ""
            if role:
                db.set_contact_job_title(contact_id, role)
    except Exception:
        log.exception("jobTitle backfill failed (continuing)")

    log.info("Research for contact %s: LinkedIn=%s api_ok=%s",
             contact_id, linkedin_url or "-", api_ok)
    return {"ok": True, "site": "research", "api_ok": api_ok, **fields}


def run(task):
    """Dispatch a task by its tag inside the one combined worker."""
    tag = task.get("tag") or "research"
    contact_id = task["contactId"]
    snap = task.get("snapshot") or {}

    if tag == "whatsapp":
        return _whatsapp(contact_id, snap)
    if tag in _PLATFORM_COLUMNS:
        return _platform(contact_id, snap, tag)
    return _research(contact_id, snap)
