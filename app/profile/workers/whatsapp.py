"""WhatsApp worker.

Derives a WhatsApp chat link from the contact's mobile number. No network
call — the number is normalized (digits only, international format) and a
'WhatsApp: https://wa.me/<number>' line is appended to other_profiles.
"""
import logging
import re

from .. import db

log = logging.getLogger("card-desk.worker.whatsapp")


def wa_number(numbers):
    """First usable number as bare international digits, or ''."""
    for num in numbers or []:
        digits = re.sub(r"[^0-9]", "", str(num or ""))
        if digits:
            return digits
    return ""


def run(task):
    contact_id = task["contactId"]
    snap = task.get("snapshot") or {}
    numbers = (snap.get("mobileNumbers") or
               snap.get("telephoneNumbers") or [])
    number = wa_number(numbers)
    if not number:
        log.info("WhatsApp for contact %s: no mobile number", contact_id)
        return {"ok": True, "site": "whatsapp", "whatsapp_url": ""}

    url = "https://wa.me/" + number
    db.append_profile_line(contact_id, "whatsapp", url)
    log.info("WhatsApp for contact %s: %s", contact_id, url)
    return {"ok": True, "site": "whatsapp", "whatsapp_url": url}
