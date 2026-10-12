from app.subsidiary import ai


def test_score_phrases_batches_all_phrases_into_one_openai_call(monkeypatch):
    calls = []

    def call(prompt, schema_name, schema, max_tokens):
        calls.append((prompt, schema_name, max_tokens))
        return {"scores": [
            {"phrase": "lt cable", "subsidiaries": {"Laser": "HIGH", "GMD": "NONE"}},
            {"phrase": "gate valve", "subsidiaries": {"Laser": "NONE", "GMD": "MEDIUM"}},
        ]}

    monkeypatch.setattr(ai, "_call", call)
    subsidiaries = [
        {"subsidiary": "Laser", "phrases": ["lt cable"]},
        {"subsidiary": "GMD", "phrases": ["gate valve"]},
    ]
    result = ai.score_phrases(["lt cable", "gate valve"], subsidiaries)
    assert len(calls) == 1
    assert result == {
        "lt cable": {"Laser": 1.0, "GMD": 0.0},
        "gate valve": {"Laser": 0.0, "GMD": 0.66},
    }
