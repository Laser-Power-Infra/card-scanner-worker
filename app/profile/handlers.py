"""Tag dispatch for the profile-collection queue.

The Next.js publisher sends every profile task to the single
'CS-profile:collection' queue with a 'tag' in the payload. This module looks
at the tag and hands the task to the combined worker — one worker handles
every tag (research / linkedin / facebook / twitter / instagram / whatsapp).
It also adapts the publisher's CardData payload into the
{contactId, tag, snapshot} shape the worker expects.
"""
import logging

from .workers import address, combined

log = logging.getLogger("card-desk.handlers")

# Tags handled by the combined worker. "address" is handled separately.
TAGS = ("research", "linkedin", "whatsapp", "instagram", "facebook", "twitter")


def snapshot_from_contact(contact):
    contact = contact or {}
    return {
        "fullName": contact.get("fullName"),
        "company": contact.get("company"),
        "jobTitle": contact.get("jobTitle"),
        "emails": contact.get("emails") or [],
        "website": contact.get("website"),
        "linkedin": contact.get("linkedin"),
        "otherSocials": contact.get("otherSocials") or [],
        "mobileNumbers": contact.get("mobileNumbers") or [],
        "telephoneNumbers": contact.get("telephoneNumbers") or [],
        "address": contact.get("address"),
        "companyLocation": contact.get("companyLocation"),
    }


def _task_from_payload(payload):
    return {
        "contactId": payload.get("contactId"),
        "tag": payload.get("tag"),
        "snapshot": snapshot_from_contact(payload.get("contact")),
    }


def handle_combined(payload):
    return combined.run(_task_from_payload(payload))


def handle_address(payload):
    return address.run(_task_from_payload(payload))


DISPATCH = {tag: handle_combined for tag in TAGS if tag != "address"}
DISPATCH["address"] = handle_address

# Human-facing worker name per tag, for console debugging of the assignment.
TAG_WORKERS = {tag: "workers.combined(%s)" % tag for tag in TAGS
               if tag != "address"}
TAG_WORKERS["address"] = "workers.address"


def dispatch(tag, payload):
    """Route a message to the combined worker by tag. Returns None for unknown
    tags."""
    handler = DISPATCH.get(tag)
    if handler is None:
        log.warning("No handler for tag %r", tag)
        return None
    log.info(">>> worker called: %s (contact %s, %s)",
             TAG_WORKERS.get(tag), payload.get("contactId"),
             (payload.get("contact") or {}).get("fullName") or "-")
    return handler(payload)


def process_payload(payload):
    """Process one decoded task payload exactly as the queue receiver does.
    Shared by the RabbitMQ receiver and the publish_sample direct-run mode so
    both behave identically.

    Returns one of:
        ("ok", result)            worker ran and returned its result dict
        ("skipped", reason)       no contactId / unknown tag (receiver drops it)
        ("error", str)            handler raised an exception
    """
    if not payload.get("contactId"):
        return ("skipped", "no contactId")
    try:
        result = dispatch(payload.get("tag"), payload)
    except Exception as exc:
        log.exception("Handler failed for tag %r (contact %s)",
                      payload.get("tag"), payload.get("contactId"))
        return ("error", str(exc))
    if result is None:
        return ("skipped", "unknown tag")
    return ("ok", result)
