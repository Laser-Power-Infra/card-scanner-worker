"""Raw SQL persistence for Prisma-owned subsidiary matching tables."""
import json
import logging
from uuid import uuid4

import psycopg

from ..shared import db as shared_db
from . import config

log = logging.getLogger("subsidiary.db")


def _conn():
    return shared_db.get_conn(config.DATABASE_URL)


def _run(sql, params=None):
    try:
        with _conn().cursor() as cursor:
            cursor.execute(sql, params)
    except psycopg.OperationalError:
        shared_db.reset_conn()
        with _conn().cursor() as cursor:
            cursor.execute(sql, params)


def _fetchone(sql, params=None):
    with _conn().cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


def _fetchall(sql, params=None):
    with _conn().cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def get_subsidiaries():
    rows = _fetchall(
        'SELECT "subsidiary", "phrase", "category", "role", '
        '"negativeStrength", "aliasGroup", "threshold" '
        'FROM subsidiary_keyword ORDER BY "subsidiary", "phrase"')
    grouped = {}
    for row in rows:
        item = grouped.setdefault(row["subsidiary"], {"subsidiary": row["subsidiary"],
            "threshold": row["threshold"], "phrases": [], "keywords": []})
        item["phrases"].append(row["phrase"])
        record = dict(row)
        record["subsidiary"] = row["subsidiary"]
        item["keywords"].append(record)
    return list(grouped.values())


def header(company):
    return _fetchone(
        'SELECT * FROM company_keyword_score WHERE "company" = %s AND "keyword" = %s',
        (company, ""))


def has_extraction(company):
    """True when a company has an OK header carrying extracted phrases.

    Used by the sweep to route already-extracted companies to the cheap
    aggregate-only path instead of a fresh search/extract.
    """
    row = header(company)
    if not row or row.get("status") != "OK":
        return False
    sources = row.get("sources") or {}
    if isinstance(sources, str):
        sources = json.loads(sources)
    return bool(sources.get("phrases"))


def get_keyword_vector_caches(phrases):
    """Newest merged vector for each phrase, fetched in ONE query.

    Reads the indexed per-phrase rows (``keyword = ANY(...)``) newest-first so
    a phrase already scored by any company is reused instead of re-sent to the
    LLM, without paying one round trip per phrase.
    """
    keys = [p for p in dict.fromkeys(phrases) if p]
    if not keys:
        return {}
    rows = _fetchall(
        'SELECT "keyword", "scores", "scoreVersions" FROM company_keyword_score '
        'WHERE "keyword" = ANY(%s) ORDER BY "fetchedAt" DESC', (keys,))
    configured_names = set(get_subsidiary_names())
    merged = {}
    for row in rows:
        phrase = row["keyword"]
        bucket = merged.setdefault(phrase, {"scores": {}, "scoreVersions": {}})
        scores = row.get("scores") or {}
        versions = row.get("scoreVersions") or {}
        if isinstance(scores, str):
            scores = json.loads(scores)
        if isinstance(versions, str):
            versions = json.loads(versions)
        for name, score in scores.items():
            if name in configured_names and name not in bucket["scores"]:
                bucket["scores"][name] = score
                if name in versions:
                    bucket["scoreVersions"][name] = versions[name]
    return {p: v for p, v in merged.items() if v["scores"]}


def get_keyword_vector_cache(phrase):
    """Newest merged vector for a single phrase (see the batch helper)."""
    return get_keyword_vector_caches([phrase]).get(phrase)


def all_company_names():
    return [row["company"] for row in _fetchall(
        'SELECT DISTINCT "company" FROM "Contact" '
        'WHERE "company" IS NOT NULL AND btrim("company") <> %s ORDER BY "company"', ("",))]


def get_contact_hints(company):
    rows = _fetchall(
        'SELECT "companyLocation", "address", "website" FROM "Contact" '
        'WHERE "company" = %s ORDER BY "createdAt" DESC LIMIT 20', (company,))
    if not rows:
        return {"state": "", "domains": []}
    state = next((r.get("companyLocation") or r.get("address") for r in rows
                  if r.get("companyLocation") or r.get("address")), "")
    domains = []
    from urllib.parse import urlparse
    for row in rows:
        website = str(row.get("website") or "").strip()
        if website:
            host = urlparse(website if "://" in website else "https://" + website).hostname
            if host:
                domains.append(host)
    return {"state": state, "domains": list(dict.fromkeys(domains))}


def get_company_sources(company):
    row = header(company)
    if not row:
        return {}
    sources = row.get("sources") or {}
    return json.loads(sources) if isinstance(sources, str) else sources


def persist_search_results(company, extract_version, sources):
    upsert_header(company, "OK", extract_version, {"results": sources})


def upsert_header(company, status, extract_version, sources):
    existing = header(company)
    if existing:
        prior = existing.get("sources") or {}
        if isinstance(prior, str):
            prior = json.loads(prior)
        if isinstance(sources, dict) and isinstance(prior, dict):
            merged = dict(prior)
            merged.update(sources)
            sources = merged
    _run(
        'INSERT INTO company_keyword_score '
        '("id", "company", "keyword", "status", "extractVersion", "sources", "fetchedAt") '
        'VALUES (%s, %s, %s, %s::"CompanyScoreStatus", %s, %s::jsonb, now()) '
        'ON CONFLICT ("company", "keyword") DO UPDATE SET '
        '"status" = EXCLUDED."status", "extractVersion" = EXCLUDED."extractVersion", '
        '"sources" = EXCLUDED."sources", "fetchedAt" = now()',
        ("ckh_" + uuid4().hex, company, "", status,
         extract_version, json.dumps(sources)))


def _executemany(sql, rows):
    if not rows:
        return
    try:
        with _conn().cursor() as cur:
            cur.executemany(sql, rows)
    except psycopg.OperationalError:
        shared_db.reset_conn()
        with _conn().cursor() as cur:
            cur.executemany(sql, rows)


def save_phrases(records):
    """Insert/update per-phrase vector rows in one round trip.

    records: iterable of (company, phrase, scores_json, extract_version,
    versions_json).
    """
    rows = [("ckp_" + uuid4().hex, company, phrase, scores_json, extract_version,
             versions_json)
            for company, phrase, scores_json, extract_version, versions_json in records]
    _executemany(
        'INSERT INTO company_keyword_score '
        '("id", "company", "keyword", "scores", "extractVersion", "scoreVersions", "fetchedAt") '
        'VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, now()) '
        'ON CONFLICT ("company", "keyword") DO UPDATE SET '
        '"scores" = EXCLUDED."scores", "extractVersion" = EXCLUDED."extractVersion", '
        '"scoreVersions" = EXCLUDED."scoreVersions", "fetchedAt" = now()',
        rows)


def save_phrase(company, phrase, scores, extract_version, score_versions):
    save_phrases([(company, phrase, json.dumps(scores), extract_version,
                   json.dumps(score_versions))])


def write_vectors(company, phrases, vectors, extract_version, score_versions):
    """Store phrase vectors in the header's reusable JSON cache and company rows.

    This keeps the approved two-table schema while ensuring repeated phrases
    across company strings reuse one persisted vector. Both the cross-company
    cache read and the per-phrase writes are batched into single round trips.
    """
    sources = get_company_sources(company) or {}
    cache = sources.get("keywordVectors", {})
    configured_names = set(get_subsidiary_names())
    wanted = [p for p in phrases if vectors.get(p)]
    shared = get_keyword_vector_caches(
        [p for p in wanted if p not in cache])
    records = []
    for phrase in wanted:
        vector = vectors[phrase]
        versions = {name: score_versions[name] for name in vector
                    if name in score_versions and name in configured_names}
        prior = shared.get(phrase) or cache.get(phrase) or {}
        merged_scores = dict(prior.get("scores") or {})
        merged_versions = dict(prior.get("scoreVersions") or {})
        filtered_vector = {name: value for name, value in vector.items()
                           if name in configured_names}
        merged_scores.update(filtered_vector)
        merged_versions.update({name: version for name, version in versions.items()
                                if name in configured_names})
        cache[phrase] = {"scores": merged_scores, "scoreVersions": merged_versions}
        records.append((company, phrase, json.dumps(filtered_vector), extract_version,
                        json.dumps({name: version for name, version in versions.items()
                                    if name in configured_names})))
    save_phrases(records)
    sources["keywordVectors"] = cache
    upsert_header(company, "OK", extract_version, sources)


def get_subsidiary_names():
    return [row["subsidiary"] for row in _fetchall(
        'SELECT DISTINCT "subsidiary" FROM subsidiary_keyword ORDER BY "subsidiary"')]


def get_phrase_rows(company):
    return _fetchall(
        'SELECT "keyword", "scores", "scoreVersions" FROM company_keyword_score '
        'WHERE "company" = %s AND "keyword" <> %s', (company, ""))


def company_matches(company):
    row = _fetchone(
        'SELECT "subsidiaryMatches" FROM "Contact" WHERE "company" = %s '
        'AND "subsidiaryMatchedAt" IS NOT NULL LIMIT 1', (company,))
    return list(row.get("subsidiaryMatches") or []) if row else None


def find_shared_vector(phrase):
    return get_keyword_vector_cache(phrase)


def write_matches(company, subsidiaries):
    _run(
        'UPDATE "Contact" SET "subsidiaryMatches" = %s, "subsidiaryMatchedAt" = now() '
        'WHERE "company" = %s', (subsidiaries, company))


def record_review(company, reason, phrases, shared_phrases, extract_version):
    """Upsert a company into the human-review queue (company_review).

    Written when extraction succeeded but nothing matched ('no_match'), or the
    source text was empty ('no_data'). ``shared_phrases`` explains any phrases
    downweighted for being owned by several subsidiaries.
    """
    _run(
        'INSERT INTO company_review '
        '("id", "company", "reason", "phrases", "sharedPhrases", "extractVersion", '
        '"createdAt", "updatedAt") '
        'VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, now(), now()) '
        'ON CONFLICT ("company") DO UPDATE SET '
        '"reason" = EXCLUDED."reason", "phrases" = EXCLUDED."phrases", '
        '"sharedPhrases" = EXCLUDED."sharedPhrases", '
        '"extractVersion" = EXCLUDED."extractVersion", "updatedAt" = now()',
        ("cr_" + uuid4().hex, company, reason, json.dumps(phrases),
         json.dumps(shared_phrases), extract_version))


def clear_matches(company):
    _run('UPDATE "Contact" SET "subsidiaryMatches" = ARRAY[]::text[], '
         '"subsidiaryMatchedAt" = now() WHERE "company" = %s', (company,))


def pending_companies(extract_version, rebuild=False):
    # --rebuild requeues only companies with a successful extraction so the
    # worker recomputes current scores/matches from cached artifacts.
    if rebuild:
        rows = _fetchall(
            'SELECT DISTINCT c."company" FROM "Contact" c '
            'JOIN company_keyword_score h ON h."company" = c."company" AND h."keyword" = %s '
            'WHERE c."company" IS NOT NULL AND btrim(c."company") <> %s '
            'AND h."status" = %s::"CompanyScoreStatus" ORDER BY c."company"',
            ("", "", "OK"))
        return [row["company"] for row in rows]
    rows = _fetchall(
        'SELECT DISTINCT c."company" FROM "Contact" c '
        'LEFT JOIN company_keyword_score h ON h."company" = c."company" AND h."keyword" = %s '
        'AND h."extractVersion" = %s '
        'WHERE c."company" IS NOT NULL AND btrim(c."company") <> %s '
        'AND (h."id" IS NULL OR (h."status" = %s::"CompanyScoreStatus" AND '
        '(c."subsidiaryMatchedAt" IS NULL OR c."subsidiaryMatches" = ARRAY[]::text[]))) '
        'ORDER BY c."company"', ("", extract_version, "", "OK"))
    return [row["company"] for row in rows]


def close():
    shared_db.close_conn()
