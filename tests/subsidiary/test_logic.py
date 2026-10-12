from app.subsidiary.logic import (aggregate, extract_version, score_versions,
                                  vocabulary_version)


def _fixture(threshold=0.5):
    subsidiaries = [{
        "subsidiary": "Laser",
        "threshold": threshold,
        "phrases": ["lt cable", "automotive cable", "government tender"],
        "keywords": [
            {"phrase": "lt cable", "role": "OWN_BUSINESS", "aliasGroup": "cable",
             "negativeStrength": None},
            {"phrase": "automotive cable", "role": "OWN_BUSINESS", "aliasGroup": None,
             "negativeStrength": "VETO"},
            {"phrase": "government tender", "role": "PROCUREMENT", "aliasGroup": None,
             "negativeStrength": None},
        ],
    }]
    return subsidiaries


def test_aliases_count_once_and_procurement_does_not_score():
    subsidiaries = _fixture()
    matches = aggregate(
        ["lt cable", "automotive cable", "government tender"],
        {"lt cable": {"Laser": 0.66}, "automotive cable": {"Laser": 0.0},
         "government tender": {"Laser": 1.0}},
        subsidiaries,
    )
    assert matches == ["Laser"]


def test_negative_keywords_no_longer_block_a_match():
    # A VETO keyword on an incidental product line must not kill an otherwise
    # genuine match; the negative keyword is inert.
    subsidiaries = _fixture()
    matches = aggregate(
        ["lt cable", "automotive cable"],
        {"lt cable": {"Laser": 1.0}, "automotive cable": {"Laser": 1.0}},
        subsidiaries,
    )
    assert matches == ["Laser"]


def test_threshold_and_version_scoping():
    subsidiaries = _fixture(threshold=0.7)
    matches = aggregate(["lt cable"], {"lt cable": {"Laser": 0.66}}, subsidiaries)
    assert matches == []
    first = score_versions("model", "prompt", subsidiaries)
    subsidiaries[0]["keywords"].append(
        {"phrase": "new phrase", "role": "OWN_BUSINESS", "aliasGroup": None,
         "negativeStrength": None})
    second = score_versions("model", "prompt", subsidiaries)
    assert first["Laser"] != second["Laser"]
    assert extract_version("model", "prompt") == extract_version("model", "prompt")


def test_score_version_ignores_unversioned_tuning_but_tracks_reference_edits():
    subsidiaries = _fixture()
    baseline = score_versions("model", "prompt", subsidiaries)["Laser"]

    # threshold / weights are applied at aggregation, so they must not move the hash.
    retuned = [dict(subsidiaries[0], threshold=0.9)]
    assert score_versions("model", "prompt", retuned)["Laser"] == baseline

    # A change to a keyword's role or alias does move the hash.
    role_edited = [dict(subsidiaries[0], keywords=[dict(k) for k in subsidiaries[0]["keywords"]])]
    role_edited[0]["keywords"][0]["role"] = "DEMAND_SIDE"
    assert score_versions("model", "prompt", role_edited)["Laser"] != baseline

    # A new model or prompt moves the hash too.
    assert score_versions("other-model", "prompt", subsidiaries)["Laser"] != baseline
    assert score_versions("model", "other-prompt", subsidiaries)["Laser"] != baseline


def test_multi_label_assigns_every_match():
    subsidiaries = _fixture()
    subsidiaries.append({
        "subsidiary": "CableCo", "threshold": 0.5, "phrases": ["lt cable"],
        "keywords": [{"phrase": "lt cable", "role": "OWN_BUSINESS",
                      "aliasGroup": "cable", "negativeStrength": None}],
    })
    matches = aggregate(["lt cable"], {"lt cable": {"Laser": 1.0, "CableCo": 0.66}}, subsidiaries)
    assert matches == ["Laser", "CableCo"]


def test_shared_phrase_is_evaluated_for_each_subsidiary_role():
    subsidiaries = [
        {"subsidiary": "Laser", "threshold": 0.5, "phrases": ["power transmission"],
         "keywords": [{"phrase": "power transmission", "role": "OWN_BUSINESS",
                       "aliasGroup": None, "negativeStrength": None}]},
        {"subsidiary": "UIC", "threshold": 0.5, "phrases": ["power transmission"],
         "keywords": [{"phrase": "power transmission", "role": "DEMAND_SIDE",
                       "aliasGroup": None, "negativeStrength": None}]},
    ]
    matches = aggregate(["power transmission"],
                        {"power transmission": {"Laser": 1.0, "UIC": 1.0}},
                        subsidiaries)
    assert matches == ["Laser"]


def test_vocabulary_version_tracks_reference_edits():
    base = _fixture()
    v0 = vocabulary_version(base)

    # Editing the reference keywords must move both the vocabulary signature
    # and the extraction version (so cached extractions re-run).
    edited = [dict(base[0], keywords=[dict(k) for k in base[0]["keywords"]] + [
        {"phrase": "new keyword", "role": "OWN_BUSINESS", "aliasGroup": None,
         "negativeStrength": None}])]
    assert vocabulary_version(edited) != v0
    assert extract_version("m", "p", vocabulary_version(edited)) != \
        extract_version("m", "p", v0)

    # A no-op reorder of the same keywords is stable.
    same = [dict(base[0], keywords=list(reversed([dict(k) for k in base[0]["keywords"]])))]
    assert vocabulary_version(same) == v0


def test_vocabulary_version_is_empty_string_safe():
    assert vocabulary_version([]) == vocabulary_version([])


def test_penalty_keyword_does_not_reduce_a_match():
    subsidiaries = [{
        "subsidiary": "Laser", "threshold": 0.5, "phrases": ["lt cable", "data cable"],
        "keywords": [
            {"phrase": "lt cable", "role": "OWN_BUSINESS", "aliasGroup": None,
             "negativeStrength": None},
            {"phrase": "data cable", "role": "OWN_BUSINESS", "aliasGroup": None,
             "negativeStrength": "PENALTY"},
        ],
    }]
    matches = aggregate(["lt cable"],
                        {"lt cable": {"Laser": 1.0}, "data cable": {"Laser": 0.66}},
                        subsidiaries)
    assert matches == ["Laser"]


def test_negative_keyword_is_inert_even_when_scored_highly():
    subsidiaries = [{
        "subsidiary": "GMD", "threshold": 0.5,
        "phrases": ["industrial valves", "tyre valve"],
        "keywords": [
            {"phrase": "industrial valves", "role": "OWN_BUSINESS", "aliasGroup": None,
             "negativeStrength": None},
            {"phrase": "tyre valve", "role": "OWN_BUSINESS", "aliasGroup": None,
             "negativeStrength": "VETO"},
        ],
    }]
    matches = aggregate(
        ["industrial valves"],
        {"industrial valves": {"GMD": 1.0}, "tyre valve": {"GMD": 1.0}},
        subsidiaries,
    )
    assert matches == ["GMD"]
