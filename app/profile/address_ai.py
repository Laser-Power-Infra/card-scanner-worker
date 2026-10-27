"""Structured company-address extraction from retrieved page text."""
import json
import logging
import os
import re
import time

import requests

from . import config

log = logging.getLogger("card-desk.address-ai")

RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"

ADDRESS_TYPES = ("head_office", "registered", "branch", "other")


class PermanentExtractionError(RuntimeError):
    """4xx that retrying cannot fix (bad request, auth, model rejection)."""

_PO_BOX = re.compile(
    r"(?:\bp\.?\s?o\.?\s*(?:box|bag)\b|"
    r"\bpost\s*(?:box|bag)\b|"
    r"\bp\.?\s?b\.?\s*(?:no|number)?\.?\s*\d+)", re.I)


def _model(model=None):
    return model or os.environ.get("ADDRESS_MODEL") or config.ENRICH_MODEL


def _headers():
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing; add it to .env")
    return {"Authorization": "Bearer " + config.OPENAI_API_KEY,
            "Content-Type": "application/json"}


_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "address_line1": {"type": "string"},
        "address_line2": {"type": "string"},
        "city": {"type": "string"},
        "state": {"type": "string"},
        "postal_code": {"type": "string"},
        "country": {"type": "string"},
        "address_type": {"type": "string", "enum": list(ADDRESS_TYPES)},
        "evidence_url": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["address_line1", "address_line2", "city", "state",
                 "postal_code", "country", "address_type", "evidence_url",
                 "quote"],
    "additionalProperties": False,
}

_SCHEMA = {
    "type": "object",
    "properties": {"addresses": {"type": "array", "items": _ADDRESS_SCHEMA}},
    "required": ["addresses"],
    "additionalProperties": False,
}


def _output_format():
    return {"text": {"format": {"type": "json_schema",
                                "name": "company_addresses",
                                "strict": True,
                                "schema": _SCHEMA}}}


def _build_prompt(company, company_location, pages):
    joined = "\n\n---\n\n".join((pages or [])[:5])
    return (
        "Extract every distinct postal address for this company from the pages "
        "below.\n"
        "Company: %s\n"
        "Known region hint: %s\n\n"
        "Rules:\n"
        "- Return only facts present in the provided pages.\n"
        "- One entry per distinct address. Return every distinct office on the "
        "page, not only the head office.\n"
        "- Prefer head office/registered office over branches when ordering, "
        "but do not omit the others.\n"
        "- address_line1 must be a street line (building + street/number). "
        "Omit any address without one.\n"
        "- address_line2 may contain the building, floor, plot, landmark, or "
        "locality that belongs to the same address.\n"
        "- Return a street-line address even when city, state, or country is "
        "missing from the page. Leave those fields empty; the worker derives "
        "them from the PIN or the contact's location.\n"
        "- If a PIN is printed with a space (for example 700 160), return the "
        "digits exactly as printed; do not invent a different PIN.\n"
        "- address_type: one of head_office, registered, branch, other.\n"
        "- evidence_url = the exact URL of the page the address came from.\n"
        "- quote = one exact contiguous span copied verbatim from that page "
        "that contains the address. No ellipses, edits or paraphrasing.\n"
        "- Never return a PO box / post bag as the street line. If the only "
        "address is a PO box, omit it.\n"
        "- Ignore addresses belonging to other companies.\n"
        "- Leave a field empty if unknown; never guess.\n"
        "- No citations inside field values.\n\n"
        "PAGES:\n%s\n\n"
        "Respond with ONLY the JSON object."
    ) % (company or "Unknown", company_location or "unknown", joined or "(none)")


def _temperature():
    value = (config.ADDRESS_TEMPERATURE or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _call_once(company, company_location, pages, model, timeout):
    temperature = _temperature()
    payload = {
        "model": model,
        "input": _build_prompt(company, company_location, pages),
        "max_output_tokens": config.ADDRESS_MAX_OUTPUT_TOKENS,
        **_output_format(),
    }
    if temperature is not None:
        payload["temperature"] = temperature

    resp = requests.post(RESPONSES_ENDPOINT, headers=_headers(),
                         json=payload, timeout=timeout)
    if (resp.status_code == 400 and "temperature" in resp.text.lower()
            and "temperature" in payload):
        log.warning("model rejected temperature; extraction is no longer "
                    "deterministic across runs")
        payload.pop("temperature", None)
        resp = requests.post(RESPONSES_ENDPOINT, headers=_headers(),
                             json=payload, timeout=timeout)
    if resp.status_code in (400, 401, 403, 404, 422):
        raise PermanentExtractionError(
            "API %s: %s" % (resp.status_code, resp.text[:200]))
    if resp.status_code != 200:
        raise RuntimeError("API %s: %s" % (resp.status_code, resp.text[:200]))
    result = resp.json()
    raw = ""
    for item in result.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text" and block.get("text"):
                raw += block["text"]
    if not raw:
        raise RuntimeError("empty model output")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("no JSON object in model output")
    data = json.loads(raw[start:end + 1])

    addresses = []
    for entry in data.get("addresses") or []:
        if not isinstance(entry, dict):
            continue
        cleaned = {}
        for key in ("address_line1", "address_line2", "city", "state",
                    "postal_code", "country", "evidence_url", "quote"):
            cleaned[key] = str(entry.get(key) or "").strip()
        address_type = str(entry.get("address_type") or "other").strip().lower()
        cleaned["address_type"] = (address_type if address_type in ADDRESS_TYPES
                                   else "other")
        addresses.append(cleaned)
    return addresses


def strip_po_box(fields):
    """Remove PO box / post bag segments from the address lines."""
    cleaned = dict(fields or {})

    def strip_field(value):
        segments = [segment.strip() for segment in str(value or "").split(",")]
        return ", ".join(segment for segment in segments
                         if segment and not _PO_BOX.search(segment))

    cleaned["address_line1"] = strip_field(cleaned.get("address_line1"))
    cleaned["address_line2"] = strip_field(cleaned.get("address_line2"))
    return cleaned


def extract_company_addresses(company, company_location, pages, *,
                              model=None, timeout=120, retries=2):
    """Returns a list of address dicts (possibly empty) from the pages."""
    model = _model(model)
    last_error = None
    for attempt in range(retries + 1):
        try:
            return _call_once(company, company_location, pages, model, timeout)
        except PermanentExtractionError as exc:
            log.error("address extraction permanent failure: %s", str(exc)[:200])
            return []
        except Exception as exc:  # noqa: BLE001 - retry boundary
            last_error = exc
            log.warning("address extraction failed (attempt %d): %s",
                        attempt + 1, str(exc)[:200])
            if attempt < retries:
                time.sleep(1)
    log.error("address extraction gave up: %s", str(last_error)[:200])
    return []


def extract_company_address(company, company_location, pages, **kwargs):
    """Compatibility wrapper: the first extracted address, or None."""
    addresses = extract_company_addresses(company, company_location, pages,
                                          **kwargs)
    return addresses[0] if addresses else None
