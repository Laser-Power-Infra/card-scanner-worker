"""Process one distinct company per RabbitMQ message."""
import json
import logging

from . import ai, config, db, logic, search

log = logging.getLogger("subsidiary.handlers")
EXTRACT_PROMPT_VERSION = "industry-phrase-extraction-v2"
SCORE_PROMPT_VERSION = "subsidiary-phrase-scoring-v1"


def _source_domain_hint(sources):
    domains = []
    for source in sources:
        url = source.get("url", "")
        if url:
            from urllib.parse import urlparse
            host = urlparse(url).hostname
            if host:
                domains.append(host)
    return list(dict.fromkeys(domains))


def _search_query(company, hint):
    query = company
    if hint.get("state"):
        query += " " + hint["state"]
    query += " company products business industry"
    return query


def aggregate_company(company, *, dry_run=False):
    header = db.header(company)
    if not header:
        return "skipped", "no company header"
    source_data = header.get("sources") or {}
    if isinstance(source_data, str):
        source_data = json.loads(source_data)
    phrases = source_data.get("phrases") or []
    if header.get("status") == "NO_DATA":
        current_matches = db.company_matches(company)
        if not dry_run and current_matches not in ([], None):
            db.clear_matches(company)
        return "ok", {"company": company, "matches": [], "status": "NO_DATA"}
    subsidiaries = db.get_subsidiaries()
    score_version_map = logic.score_versions(config.LLM_MODEL, SCORE_PROMPT_VERSION, subsidiaries)
    vectors = {}
    for phrase in phrases:
        cached = ((source_data.get("keywordVectors") or {}).get(phrase))
        candidate = cached or db.get_keyword_vector_cache(phrase)
        if candidate:
            vectors[phrase] = dict(logic.version_valid_scores(candidate, score_version_map))
    matches = logic.aggregate(phrases, vectors, subsidiaries)
    current_matches = db.company_matches(company)
    if not dry_run and current_matches is None:
        db.write_matches(company, matches)
    return "ok", {"company": company, "matches": matches, "status": header.get("status")}


def process_company(payload, *, dry_run=False):
    company = str(payload.get("company") or "").strip()
    if not company:
        return "skipped", "missing company"
    hint = payload.get("hint") or {}
    subsidiaries = db.get_subsidiaries()
    if not subsidiaries:
        return "error", "subsidiary_keyword is empty; run seed_reference.py"

    if payload.get("aggregate_only"):
        return aggregate_company(company, dry_run=dry_run)

    extract_version = logic.extract_version(
        config.LLM_MODEL, EXTRACT_PROMPT_VERSION,
        logic.vocabulary_version(subsidiaries))
    score_version_map = logic.score_versions(config.LLM_MODEL, SCORE_PROMPT_VERSION, subsidiaries)
    header = db.header(company)
    prior_extract_version = header.get("extractVersion") if header else None

    if header and header.get("status") == "NO_DATA":
        current_matches = db.company_matches(company)
        if not dry_run and current_matches not in ([], None):
            db.clear_matches(company)
        return "ok", {"company": company, "status": "NO_DATA", "cached": True, "matches": []}

    if header and header.get("status") != "NO_DATA":
        source_data = header.get("sources") or {}
        if isinstance(source_data, str):
            source_data = json.loads(source_data)
        if (header.get("extractVersion") != extract_version
                or "phrases" not in source_data):
            # A changed extraction prompt reuses persisted search results.
            sources = source_data.get("results", [])
            if sources:
                phrases = ai.extract_phrases(company, hint.get("state", ""), sources,
                                             subsidiaries)
                source_data["phrases"] = phrases
                if not dry_run:
                    db.upsert_header(company, "OK", extract_version, source_data)
            else:
                phrases = []
        else:
            phrases = source_data.get("phrases")
        sources = source_data.get("results", [])
    else:
        query = _search_query(company, hint)
        source_data = db.get_company_sources(company)
        sources = source_data.get("results", [])
        if not sources:
            domains = hint.get("domains") or []
            try:
                sources = search.search(query, domains=domains)
            except search.NoSearchResults:
                sources = []
        text = " ".join(item.get("text", "") for item in sources).strip()
        if not text:
            source_data = {"query": query, "results": sources, "phrases": []}
            if not dry_run:
                db.upsert_header(company, "NO_DATA", extract_version, source_data)
                db.clear_matches(company)
            return "ok", {"company": company, "status": "NO_DATA", "matches": []}
        source_data = {**source_data, "query": query, "results": sources}
        # Persist raw search results before extraction so prompt changes can
        # re-extract without paying for another search call.
        if not dry_run:
            db.persist_search_results(company, extract_version, sources)
        phrases = ai.extract_phrases(company, hint.get("state", ""), sources,
                                     subsidiaries)
        source_data["phrases"] = phrases
        if not dry_run:
            db.upsert_header(company, "OK", extract_version, source_data)

    if not phrases:
        if not dry_run:
            db.upsert_header(company, "NO_DATA", extract_version,
                             {**source_data, "phrases": []})
            db.clear_matches(company)
        return "ok", {"company": company, "status": "NO_DATA", "matches": []}

    if not dry_run:
        db.upsert_header(company, "OK", extract_version,
                         {**source_data, "phrases": phrases})

    header_vectors = source_data.get("keywordVectors", {})
    vectors = {}
    stale_phrases = []
    for phrase in phrases:
        cached = header_vectors.get(phrase)
        if cached:
            vectors[phrase] = logic.version_valid_scores(cached, score_version_map)
        shared = None if cached else db.get_keyword_vector_cache(phrase)
        if shared:
            shared_scores = logic.version_valid_scores(shared, score_version_map)
            vectors.setdefault(phrase, {}).update(shared_scores)
        missing = [name for name in score_version_map if name not in vectors.get(phrase, {})]
        if missing:
            stale_phrases.append(phrase)

    rescored = False
    if stale_phrases:
        scored = ai.score_phrases(stale_phrases, subsidiaries)
        rescored = bool(scored)
        for phrase, vector in scored.items():
            vectors.setdefault(phrase, {}).update(vector)

    if not dry_run:
        db.write_vectors(company, phrases, vectors, extract_version, score_version_map)

    matches = logic.aggregate(phrases, vectors, subsidiaries)
    current_matches = db.company_matches(company)
    if not dry_run and (rescored or current_matches is None or prior_extract_version != extract_version):
        db.write_matches(company, matches)
    return "ok", {"company": company, "status": "OK", "phrase_count": len(phrases),
                   "matches": matches, "cached": not bool(stale_phrases)}


def process_payload(payload, dry_run=False):
    try:
        return process_company(payload, dry_run=dry_run)
    except search.SearchExhausted as exc:
        return "paused", str(exc)
    except Exception as exc:
        log.exception("Matching task failed for %r", payload.get("company"))
        return "error", str(exc)
