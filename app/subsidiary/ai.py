"""OpenAI Responses API extraction and standalone phrase relevance scoring."""
import json
import logging
import re
import time

import requests

from . import config

log = logging.getLogger("subsidiary.ai")
ENDPOINT = "https://api.openai.com/v1/responses"
BUCKETS = {"NONE": 0.0, "LOW": 0.33, "MEDIUM": 0.66, "HIGH": 1.0}


def _headers():
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return {"Authorization": "Bearer " + config.OPENAI_API_KEY,
            "Content-Type": "application/json"}


def _call(prompt, schema_name, schema, max_tokens):
    body = {
        "model": config.LLM_MODEL,
        "input": prompt,
        "max_output_tokens": max_tokens,
        "text": {"format": {"type": "json_schema", "name": schema_name,
                              "strict": True, "schema": schema}},
    }
    # Omit temperature for models that reject it (config.LLM_TEMPERATURE == "").
    if config.LLM_TEMPERATURE != "":
        body["temperature"] = float(config.LLM_TEMPERATURE)
    start = time.monotonic()
    response = requests.post(ENDPOINT, headers=_headers(), json=body, timeout=180)
    if response.status_code >= 500 or response.status_code == 429:
        raise RuntimeError("OpenAI transient error %s: %s" % (response.status_code, response.text[:200]))
    response.raise_for_status()
    result = response.json()
    text = "".join(block.get("text", "")
                   for item in result.get("output", []) if item.get("type") == "message"
                   for block in item.get("content", []) if block.get("type") == "output_text")
    if not text:
        raise ValueError("OpenAI returned no structured output")
    log.info("OpenAI %s completed in %.2fs", schema_name, time.monotonic() - start)
    return json.loads(text)


def extract_phrases(company, state, sources):
    source_text = "\n\n".join(
        "SOURCE: %s\n%s" % (item.get("url", ""), item.get("text", ""))
        for item in sources if item.get("text"))[:60000]
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"phrases": {"type": "array", "items": {"type": "string"},
                                   "maxItems": config.MATCH_MAX_PHRASES}},
        "required": ["phrases"],
    }
    prompt = (
        "Extract business-relevant phrases describing what this company actually makes, "
        "sells, or does. Prefer precise multi-word trade/industry phrases; do not output "
        "single generic words, company names, location, or procurement terms. "
        "Include customer-sector or application phrases when explicitly stated. "
        "Use vocabulary near these subsidiary keyword lists when "
        "semantically equivalent, but do not force a match. Return most specific phrases "
        "first, at most %d.\nCompany: %s\nState: %s\nSources:\n%s" %
        (config.MATCH_MAX_PHRASES, company, state or "unknown", source_text))
    data = _call(prompt, "industry_phrases", schema, 1500)
    return _canonical_phrases(data.get("phrases", []))


def score_phrases(phrases, subsidiaries):
    """Score phrases against all subsidiaries in one structured request."""
    if not phrases:
        return {}
    names = [s["subsidiary"] for s in subsidiaries]
    rubric = "\n".join(
        "%s: %s" % (s["subsidiary"], "; ".join(s["phrases"]))
        for s in subsidiaries)
    prompt = (
        "Rate how relevant each business phrase is to each subsidiary's business. "
        "Judge each phrase standalone; use the supplied company vocabulary as context. "
        "Use the exact input phrase as the output phrase value. "
        "NONE means unrelated; LOW means weak/indirect relevance; MEDIUM means clear "
        "relevance; HIGH means defining/core relevance. Do not force a match. "
        "Return one bucket per subsidiary per phrase. Keep phrase rows in input order. "
        "\nSubsidiaries:\n%s\nPhrases:\n%s" %
        (rubric, "\n".join("- " + p for p in phrases)))
    properties = {"scores": {
        "type": "array",
        "items": {"type": "object", "properties": {
            "phrase": {"type": "string"},
            "subsidiaries": {"type": "object", "properties": {
                name: {"type": "string", "enum": ["NONE", "LOW", "MEDIUM", "HIGH"]}
                for name in names
            }, "required": names, "additionalProperties": False},
        }, "required": ["phrase", "subsidiaries"], "additionalProperties": False},
    }}
    schema = {"type": "object", "properties": properties,
              "required": ["scores"], "additionalProperties": False}
    prompt += "\nScore every phrase: " + json.dumps(phrases, ensure_ascii=False)
    data = _call(prompt, "keyword_relevance_vectors", schema, 4000)
    vectors = {}
    phrase_lookup = {p.lower(): p for p in phrases}
    for row in data.get("scores", []):
        phrase = str(row.get("phrase", ""))
        canonical = phrase_lookup.get(phrase.lower(), phrase)
        vectors[canonical] = {name: BUCKETS.get(
            str(row.get("subsidiaries", {}).get(name, "NONE")).upper(), 0.0)
            for name in names}
    return vectors


def _canonical_phrases(values):
    output = []
    seen = set()
    for value in values:
        phrase = re.sub(r"\s+", " ", str(value or "").lower().strip())
        phrase = re.sub(r"[.!?,;:]+$", "", phrase)
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        output.append(phrase)
    return output
