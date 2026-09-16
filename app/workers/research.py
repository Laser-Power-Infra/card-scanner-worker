"""Research worker (tag 'research').

One task per contact. Runs OpenAI's Responses API with the built-in web_search
tool to find the person's LinkedIn/social profiles, bio, and company details,
then applies the exact-match and photo-verification rules before saving to the
enrichment table.

Mapping -> enrichment table:
  linkedin_url          -> linkedin_url
  linkedin_photo_url    -> linkedin_photo_url + avatar_url
  summary               -> summary
  linkedin_position     -> career_background (+ Current Role -> Contact.jobTitle)
  other_web_profiles    -> other_profiles
  company_details       -> company_details
  enrich_sources        -> sources
  company_core_business -> company_core_business
  official_site         -> official_site
  location              -> location
"""
import logging
import re

from .. import ai, common, db

log = logging.getLogger("card-desk.worker.research")


def run(task):
    contact_id = task["contactId"]
    snap = task.get("snapshot") or {}
    name = str(snap.get("fullName") or "").strip()
    desig = str(snap.get("jobTitle") or "").strip()
    company = str(snap.get("company") or "").strip()

    if not name and not company:
        return {"ok": False, "site": "research", "reason": "no name/company"}

    data, api_ok = ai.enrich_person(name, desig, company, worker="workers.research")

    # The AI's FB/TW/IG lines are NOT pre-verified against the person's name —
    # a company page can slip through. Drop any such line that doesn't match.
    data["other_web_profiles"] = common.keep_personal_only(
        data.get("other_web_profiles", ""), name)

    # LinkedIn EXACT-match: keep the AI link only when its slug contains the
    # person's name. A wrong profile is worse than none.
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

    # Job-title backfill: only from real AI output, only when the card printed
    # no title.
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
