"""Social worker (tags 'facebook' / 'twitter' / 'instagram').

Finds the person's Facebook / Twitter(X) / Instagram profile via AI (OpenAI
web_search) and keeps it only when the profile's path actually contains part of
the person's name (exact-match). whatsapp is handled by its own worker.
"""
import logging

from .. import ai, common, db

log = logging.getLogger("card-desk.worker.social")

_PLATFORM_COLUMNS = {
    "facebook": "facebook_url",
    "twitter": "twitter_url",
    "instagram": "instagram_url",
}


def run_platform(task, platform):
    contact_id = task["contactId"]
    snap = task.get("snapshot") or {}
    name = str(snap.get("fullName") or "").strip()
    company = str(snap.get("company") or "").strip()
    column = _PLATFORM_COLUMNS.get(platform)
    if column is None:
        return {"ok": False, "site": platform}

    url = ""
    if name:
        try:
            url = ai.find_platform_profile(
                name, company, platform,
                worker="workers.social(%s)" % platform)
        except Exception:
            log.exception("ai find_platform_profile failed (continuing)")
        if common.path_score(url, name) < 1:
            url = ""  # name-verified match only — never a random same-name page

    fields = {}
    if url:
        fields[column] = url
    db.merge_enrichment(contact_id, fields)
    db.append_profile_line(contact_id, platform, url)

    log.info("%s for contact %s: %s", platform.capitalize(), contact_id, url or "-")
    return {"ok": True, "site": platform}
