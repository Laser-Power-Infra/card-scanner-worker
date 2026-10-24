"""Structured company-address extraction from retrieved page text."""
import json
import logging
import os
import time

import requests

from . import config

log = logging.getLogger("card-desk.address-ai")

RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"


def _model(model=None):
    return model or os.environ.get("ADDRESS_MODEL") or config.ENRICH_MODEL


def _headers():
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing; add it to .env")
    return {"Authorization": "Bearer " + config.OPENAI_API_KEY,
            "Content-Type": "application/json"}


_SCHEMA = {
    "type": "object",
    "properties": {
        "address_line1": {"type": "string"},
        "address_line2": {"type": "string"},
        "city": {"type": "string"},
        "state": {"type": "string"},
        "postal_code": {"type": "string"},
        "country": {"type": "string"},
        "evidence_url": {"type": "string"},
    },
    "required": ["address_line1", "address_line2", "city", "state",
                 "postal_code", "country", "evidence_url"],
    "additionalProperties": False,
}


def _output_format():
    return {"text": {"format": {"type": "json_schema",
                                "name": "company_address",
                                "strict": True,
                                "schema": _SCHEMA}}}


def _build_prompt(company, company_location, pages):
    joined = "\n\n---\n\n".join((pages or [])[:5])
    return (
        "Extract the company's postal/headquarters address from the pages below.\n"
        "Company: %s\n"
        "Known region hint: %s\n\n"
        "Rules:\n"
        "- Return only facts present in the provided pages.\n"
        "- address_line1 = street number + street name.\n"
        "- Leave any field empty if unknown; never guess.\n"
        "- evidence_url = the URL of the page the address came from, or empty.\n"
        "- No citations inside field values.\n\n"
        "PAGES:\n%s\n\n"
        "Respond with ONLY the JSON object."
    ) % (company or "Unknown", company_location or "unknown", joined or "(none)")


def _call_once(company, company_location, pages, model, timeout):
    payload = {
        "model": model,
        "input": _build_prompt(company, company_location, pages),
        "max_output_tokens": 900,
        **_output_format(),
    }
    resp = requests.post(RESPONSES_ENDPOINT, headers=_headers(),
                         json=payload, timeout=timeout)
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
    for key in ("address_line1", "address_line2", "city", "state",
                "postal_code", "country", "evidence_url"):
        data.setdefault(key, "")
        data[key] = str(data.get(key) or "").strip()
    return data


def extract_company_address(company, company_location, pages, *,
                           model=None, timeout=120, retries=1):
    """Returns a dict of address fields, or None on failure."""
    model = _model(model)
    last_error = None
    for attempt in range(retries + 1):
        try:
            return _call_once(company, company_location, pages, model, timeout)
        except Exception as exc:  # noqa: BLE001 - retry boundary
            last_error = exc
            log.warning("address extraction failed (attempt %d): %s",
                        attempt + 1, str(exc)[:200])
            if attempt < retries:
                time.sleep(1)
    log.error("address extraction gave up: %s", str(last_error)[:200])
    return None
