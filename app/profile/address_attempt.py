"""Console records for every address attempt.

Phase 1 of the address overhaul: each exit point in workers/address.py emits
one readable, machine-parseable block. The field names mirror the future
AddressAttempt table so swapping the console emitter for an insert is a
mechanical change.

Set ADDRESS_ATTEMPT_LOG=0 to silence the records without touching code.
"""
import logging
import os
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger("card-desk.address-attempt")

PREFIX = "ADDRESS_ATTEMPT"

# (label shown in the block, key in the extracted-address dict)
_EXTRACTED_FIELDS = (
    ("line1", "address_line1"),
    ("line2", "address_line2"),
    ("city", "city"),
    ("state", "state"),
    ("pin", "postal_code"),
    ("country", "country"),
    ("evidence", "evidence_url"),
)


def enabled():
    """False when ADDRESS_ATTEMPT_LOG is 0/false/no/off."""
    value = os.environ.get("ADDRESS_ATTEMPT_LOG", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _clean(value, limit=200):
    """Collapse whitespace so one record always spans predictable lines."""
    text = " ".join(str(value or "").split())
    return text[:limit]


def _val(value):
    return "-" if value is None or value == "" else value


def start(contact_id, company="", company_location=""):
    return {
        "attemptId": uuid.uuid4().hex[:12],
        "contactId": str(contact_id or ""),
        "company": company or "",
        "companyLocation": company_location or "",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"),
        "started": time.monotonic(),
        "durationMs": None,
        "pages": [],
        "extracted": None,
        "map": None,
        "checks": [],
        "notes": [],
        "outcome": None,
        "reason": None,
        "logged": False,
    }


def page(url, text, limit=200):
    """A page reference: URL plus a short single-line snippet."""
    return {"url": _clean(url, 300), "snippet": _clean(text, limit)}


def check(name, passed, detail="", reason=""):
    return {
        "name": str(name),
        "passed": bool(passed),
        "detail": _clean(detail, 200),
        "reason": reason or name,
    }


def note(attempt, text):
    if text:
        attempt["notes"].append(_clean(text, 300))


def finish(attempt, outcome, reason, *, pages=None, extracted=None,
           map_result=None, checks=None, extra_notes=None):
    """Stamp and log one attempt. Idempotent: a second call is a no-op."""
    if attempt.get("logged"):
        return attempt
    if pages is not None:
        attempt["pages"] = list(pages)
    if extracted is not None:
        attempt["extracted"] = extracted
    if map_result is not None:
        attempt["map"] = map_result
    if checks is not None:
        attempt["checks"] = list(checks)
    for text in extra_notes or ():
        note(attempt, text)
    attempt["outcome"] = "PASS" if str(outcome).lower() == "pass" else "FAIL"
    attempt["reason"] = str(reason or "-")
    attempt["durationMs"] = int((time.monotonic() - attempt["started"]) * 1000)
    attempt["logged"] = True
    if enabled():
        log.info("%s", format_attempt(attempt))
    return attempt


def format_attempt(attempt):
    """Render the block the log parser in scripts/address_attempts.py expects."""
    duration = (attempt.get("durationMs") or 0) / 1000.0
    lines = [
        "%s %s (contact %s, %s)" % (
            PREFIX, attempt.get("attemptId") or "-",
            attempt.get("contactId") or "-",
            _clean(attempt.get("company"), 120) or "-"),
        "  time      : %s | %.1fs" % (attempt.get("at") or "-", duration),
        "  outcome   : %s" % (attempt.get("outcome") or "-"),
        "  reason    : %s" % (attempt.get("reason") or "-"),
    ]

    pages = attempt.get("pages") or []
    lines.append("  pages     : %d" % len(pages))
    for ref in pages:
        lines.append("    - %s | %s" % (
            _clean(ref.get("url"), 300) or "-",
            _clean(ref.get("snippet"), 200) or "-"))

    extracted = attempt.get("extracted")
    if extracted:
        lines.append("  extracted : " + " | ".join(
            "%s=%s" % (label, _clean(extracted.get(key), 160) or "-")
            for label, key in _EXTRACTED_FIELDS))
    else:
        lines.append("  extracted : -")

    mapped = attempt.get("map")
    if mapped:
        lines.append(
            "  map       : %s | lat=%s lon=%s | type=%s | match=%s conf=%s "
            "| place=%s" % (
                _clean(mapped.get("formatted"), 200) or "-",
                _val(mapped.get("lat")), _val(mapped.get("lon")),
                _clean(mapped.get("resultType"), 60) or "-",
                _clean(mapped.get("matchType"), 60) or "-",
                _val(mapped.get("confidence")),
                _clean(mapped.get("placeId"), 80) or "-"))
    else:
        lines.append("  map       : -")

    checks = attempt.get("checks") or []
    failed = [c for c in checks if not c.get("passed")]
    lines.append("  checks    : %d failed of %d" % (len(failed), len(checks)))
    for item in checks:
        detail = (": " + item["detail"]) if item.get("detail") else ""
        lines.append("    [%s] %s%s" % (
            "PASS" if item.get("passed") else "FAIL",
            item.get("name") or "-", detail))

    notes = attempt.get("notes") or []
    if notes:
        lines.append("  notes     : " + " ; ".join(
            _clean(n, 300) for n in notes))
    return "\n".join(lines)
