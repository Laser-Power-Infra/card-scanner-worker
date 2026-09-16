"""LinkedIn worker (tag 'linkedin').

Finds the person's LinkedIn profile via AI (OpenAI web_search) and applies the
EXACT-match rule: a profile URL only counts when its /in/ slug contains part of
the person's name. A wrong profile is worse than none.
"""
import logging

from .. import ai, common, db

log = logging.getLogger("card-desk.worker.linkedin")


def run(task):
    contact_id = task["contactId"]
    snap = task.get("snapshot") or {}
    name = str(snap.get("fullName") or "").strip()
    company = str(snap.get("company") or "").strip()

    linkedin_url = ""
    if name:
        try:
            linkedin_url = ai.find_platform_profile(
                name, company, "linkedin", worker="workers.linkedin")
        except Exception:
            log.exception("ai find_platform_profile failed (continuing)")
        if common.li_score(linkedin_url, name) < 1:
            linkedin_url = ""  # name-verified match only

    fields = {"linkedin_url": linkedin_url} if linkedin_url else {}
    db.merge_enrichment(contact_id, fields)

    log.info("LinkedIn for contact %s: %s", contact_id, linkedin_url or "-")
    return {"ok": True, "site": "linkedin", "linkedin_url": linkedin_url}
