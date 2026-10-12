"""Pure matching logic shared by the worker and its tests."""
from collections import defaultdict
import hashlib
import json
import re

from . import config

ROLE_WEIGHTS = {"OWN_BUSINESS": config.MATCH_WEIGHT_OWN_BUSINESS,
                "TECHNICAL": config.MATCH_WEIGHT_TECHNICAL,
                "DEMAND_SIDE": config.MATCH_WEIGHT_DEMAND_SIDE,
                "PROCUREMENT": 0.0}
NORMALIZATION_VERSION = "phrase-normalization-v1"


def normalize_phrase(value):
    phrase = str(value or "").lower().strip()
    phrase = phrase.replace("–", "-").replace("—", "-")
    phrase = re.sub(r"[.!?,;:]+$", "", phrase)
    phrase = re.sub(r"\s+", " ", phrase)
    phrase = re.sub(r"\s*&\s*", " and ", phrase)
    return phrase


def extract_version(model, prompt, vocabulary_sig=""):
    payload = "extract|%s|%s|%s|%s" % (
        model, prompt, NORMALIZATION_VERSION, vocabulary_sig)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def vocabulary_version(subsidiaries):
    """Signature of the reference keywords that extraction is steered toward.

    Feeding keywords into the extractor means a keyword edit must invalidate
    cached extractions; folding this into extract_version does that cheaply —
    the worker re-extracts from persisted search results, no new search.
    """
    payload = "|".join(sorted(
        normalize_phrase(item.get("phrase"))
        for subsidiary in subsidiaries
        for item in subsidiary.get("keywords", [])))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def score_versions(model, prompt, subsidiaries):
    versions = {}
    for subsidiary in subsidiaries:
        reference = sorted(
            "|".join([
                normalize_phrase(item.get("phrase")),
                str(item.get("category") or ""),
                str(item.get("role") or ""),
                str(item.get("negativeStrength") or ""),
                str(item.get("aliasGroup") or ""),
            ])
            for item in subsidiary["keywords"])
        payload = "score|%s|%s|%s|%s" % (
            model, prompt, NORMALIZATION_VERSION, "|".join(reference))
        versions[subsidiary["subsidiary"]] = hashlib.sha1(
            payload.encode("utf-8")).hexdigest()[:12]
    return versions


def aggregate(phrases, score_vectors, subsidiaries, top_n=None):
    """Return subsidiary names whose weighted max score clears threshold.

    Phrase rows should have keyword, scores, and version-valid vectors. The
    reference includes each phrase's role and aliasGroup. Negative-strategy
    keywords are inert: they never count as positives and no longer veto or
    penalise a match. Gold-set validation showed the veto/penalty step dropped
    genuine matches (incidental product lines like "automotive cable" killed
    otherwise-correct cable firms) without preventing real false positives.
    """
    top_n = top_n or config.MATCH_TOP_N
    reference = defaultdict(list)
    for subsidiary in subsidiaries:
        for item in subsidiary["keywords"]:
            reference[normalize_phrase(item["phrase"])].append(
                {**item, "subsidiary": item.get("subsidiary", subsidiary["subsidiary"])})

    matched = []
    for subsidiary in subsidiaries:
        name = subsidiary["subsidiary"]
        positives = defaultdict(float)
        subsidiary_top_n = int(subsidiary.get("topN", top_n))
        for phrase in phrases:
            key = normalize_phrase(phrase)
            items = reference.get(key, [])
            if not items:
                continue
            bucket = float((score_vectors.get(key) or {}).get(name, 0.0) or 0.0)
            target_items = [item for item in items if item["subsidiary"] == name]
            if not target_items:
                continue
            positives_only = [item for item in target_items
                              if item.get("negativeStrength") is None]
            if not positives_only:
                continue
            weights = [ROLE_WEIGHTS.get(item.get("role"), 0.0) for item in positives_only]
            weight = max(weights, default=0.0)
            if weight <= 0 or bucket <= 0:
                continue
            alias = next((item.get("aliasGroup") for item in positives_only
                          if item.get("aliasGroup")), None) or key
            positives[alias] = max(positives[alias], bucket * weight)

        best = sorted(positives.values(), reverse=True)[:subsidiary_top_n]
        score = max(best, default=0.0)
        if score >= float(subsidiary.get("threshold", 0.5)):
            matched.append(name)
    return matched


def version_valid_scores(row, current_versions):
    scores = row.get("scores") or {}
    versions = row.get("scoreVersions") or {}
    if isinstance(scores, str):
        scores = json.loads(scores)
    if isinstance(versions, str):
        versions = json.loads(versions)
    return {name: scores[name] for name, version in current_versions.items()
            if versions.get(name) == version and name in scores}
