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
        "lookups": [],
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


def candidate(index, total, *, extracted=None, address_type="",
              evidence_url="", lookups=None, checks=None, map_result=None,
              verdict="", precision="", distance_km=None, reason="",
              location_id=None, accepted=False, pin_source="",
              rejected=None):
    """One extracted candidate's own record for the attempt block."""
    return {
        "index": int(index),
        "total": int(total),
        "address_type": str(address_type or "other"),
        "evidence_url": _clean(evidence_url, 300),
        "extracted": _clean_extracted(extracted),
        "lookups": list(lookups or []),
        "checks": list(checks or []),
        "map": map_result,
        "verdict": str(verdict or "rejected"),
        "precision": str(precision or ""),
        "distance_km": distance_km,
        "reason": str(reason or "-"),
        "location_id": location_id,
        "accepted": bool(accepted),
        "pin_source": str(pin_source or ""),
        "rejected": rejected,
        "primary": False,
    }


def _clean_extracted(extracted):
    if not extracted:
        return None
    return {key: _clean(extracted.get(key), 160)
            for _, key in _EXTRACTED_FIELDS}


def note(attempt, text):
    if text:
        attempt["notes"].append(_clean(text, 300))


def finish(attempt, outcome, reason, *, pages=None, extracted=None,
           map_result=None, checks=None, lookups=None, extra_notes=None,
           candidates=None):
    """Stamp and log one attempt. Idempotent: a second call is a no-op.

    When ``candidates`` is supplied, the attempt summary is derived from them:
    the primary candidate (or the first accepted, else the last one) supplies
    the extracted/map/checks fields, and lookups are flattened across all
    candidates.
    """
    if attempt.get("logged"):
        return attempt
    if pages is not None:
        attempt["pages"] = list(pages)
    if candidates is not None:
        attempt["candidates"] = list(candidates)
    chosen = None
    if attempt.get("candidates"):
        chosen = next((item for item in attempt["candidates"]
                       if item.get("primary")), None)
        if chosen is None:
            chosen = next((item for item in attempt["candidates"]
                           if item.get("accepted")), None)
        if chosen is None:
            chosen = attempt["candidates"][-1]
    if extracted is not None:
        attempt["extracted"] = extracted
    elif chosen and not attempt.get("extracted"):
        attempt["extracted"] = chosen.get("extracted")
    if map_result is not None:
        attempt["map"] = map_result
    elif chosen and not attempt.get("map"):
        attempt["map"] = chosen.get("map")
    if checks is not None:
        attempt["checks"] = list(checks)
    elif chosen and not attempt.get("checks"):
        attempt["checks"] = list(chosen.get("checks") or [])
    if lookups is not None:
        attempt["lookups"] = list(lookups)
    elif attempt.get("candidates") and not attempt.get("lookups"):
        attempt["lookups"] = [item for cand in attempt["candidates"]
                              for item in (cand.get("lookups") or [])]
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
            "| precision=%s | place=%s" % (
                _clean(mapped.get("formatted"), 200) or "-",
                _val(mapped.get("lat")), _val(mapped.get("lon")),
                _clean(mapped.get("resultType"), 60) or "-",
                _clean(mapped.get("matchType"), 60) or "-",
                _val(mapped.get("confidence")),
                _clean(mapped.get("precision"), 20) or "-",
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

    review = attempt.get("review") or []
    if review:
        lines.append("  review    : " + " ; ".join(
            _clean(item, 120) for item in review))

    notes = attempt.get("notes") or []
    if notes:
        lines.append("  notes     : " + " ; ".join(
            _clean(n, 300) for n in notes))

    lookups = attempt.get("lookups") or []
    if lookups:
        rendered = []
        for item in lookups:
            text = "%s:%s/%s" % (item.get("kind") or "-",
                                 item.get("results", 0),
                                 item.get("best") or "-")
            if item.get("precision"):
                text += "/%s" % item["precision"]
            if item.get("distance_km") is not None:
                text += "@%.1fkm" % float(item["distance_km"])
            rendered.append(text)
        lines.append("  lookups   : " + " | ".join(rendered))
        worked = next((item.get("kind") for item in lookups
                       if item.get("worked")), "-")
        lines.append("              worked: %s" % worked)

    candidates = attempt.get("candidates") or []
    if candidates:
        saved = sum(1 for item in candidates if item.get("accepted"))
        primary = next((item for item in candidates if item.get("primary")), None)
        lines.append("  candidates: %d (saved=%d, primary=%s)"
                     % (len(candidates), saved,
                        primary.get("index") if primary else "-"))
        for item in candidates:
            lines.append("  candidate %s/%s:" % (
                item.get("index") or "?", item.get("total") or "?"))
            lines.append("    type      : %s" % (item.get("address_type") or "-"))
            extracted = item.get("extracted")
            lines.append("    extracted : " + (
                " | ".join("%s=%s" % (label, _val(extracted.get(key)))
                           for label, key in _EXTRACTED_FIELDS)
                if extracted else "-"))
            mapped = item.get("map")
            if mapped:
                lines.append(
                    "    map       : %s | lat=%s lon=%s | type=%s | "
                    "precision=%s" % (
                        _clean(mapped.get("formatted"), 160) or "-",
                        _val(mapped.get("lat")), _val(mapped.get("lon")),
                        _clean(mapped.get("resultType"), 40) or "-",
                        _clean(mapped.get("precision"), 20) or "-"))
            else:
                lines.append("    map       : -")
            checks = item.get("checks") or []
            failed = [entry for entry in checks if not entry.get("passed")]
            lines.append("    checks    : %d failed of %d"
                         % (len(failed), len(checks)))
            for entry in checks:
                detail = (": " + entry["detail"]) if entry.get("detail") else ""
                lines.append("      [%s] %s%s" % (
                    "PASS" if entry.get("passed") else "FAIL",
                    entry.get("name") or "-", detail))
            item_lookups = item.get("lookups") or []
            if item_lookups:
                rendered = []
                for lookup in item_lookups:
                    text = "%s:%s/%s" % (lookup.get("kind") or "-",
                                         lookup.get("results", 0),
                                         lookup.get("best") or "-")
                    if lookup.get("precision"):
                        text += "/%s" % lookup["precision"]
                    if lookup.get("distance_km") is not None:
                        text += "@%.1fkm" % float(lookup["distance_km"])
                    rendered.append(text)
                lines.append("    lookups   : " + " | ".join(rendered))
                for lookup in item_lookups:
                    options = lookup.get("options") or []
                    if not options:
                        continue
                    rendered = []
                    for option in options:
                        distance = option.get("distance_km")
                        rendered.append(
                            "%.0f/%s/%s%s %s" % (
                                option.get("score") or 0,
                                option.get("outcome") or "-",
                                option.get("precision") or "-",
                                ("@%.1fkm" % distance
                                 if distance is not None else ""),
                                _clean(option.get("formatted"), 70) or "-"))
                    lines.append("      %-8s: %s" % (
                        lookup.get("kind") or "-", " | ".join(rendered)))
            distance = item.get("distance_km")
            lines.append(
                "    verdict   : %s/%s | distance=%s | reason=%s | "
                "pin_source=%s | saved=%s"
                % (item.get("verdict") or "-", item.get("precision") or "-",
                   ("%.1fkm" % distance) if distance is not None else "-",
                   item.get("reason") or "-",
                   item.get("pin_source") or "-",
                   item.get("location_id") or "-"))
            rejected = item.get("rejected")
            if rejected:
                lines.append(
                    "    rejected  : %s | %s,%s | type=%s | precision=%s"
                    % (_clean(rejected.get("formatted"), 140) or "-",
                       _val(rejected.get("lat")), _val(rejected.get("lon")),
                       _clean(rejected.get("resultType"), 40) or "-",
                       _clean(rejected.get("precision"), 20) or "-"))
    return "\n".join(lines)
