from app.subsidiary import ai

import pytest


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _message(text):
    return {"output": [{"type": "message",
                        "content": [{"type": "output_text", "text": text}]}]}


def test_call_retries_once_on_empty_output(monkeypatch):
    calls = {"n": 0}

    def post(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse({"output": []})            # empty -> ValueError
        return _FakeResponse(_message('{"ok": true}'))

    monkeypatch.setattr(ai, "_headers", lambda: {})
    monkeypatch.setattr(ai.requests, "post", post)

    assert ai._call("p", "s", {"type": "object"}, 10) == {"ok": True}
    assert calls["n"] == 2


def test_call_raises_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr(ai, "_headers", lambda: {})
    monkeypatch.setattr(ai.requests, "post",
                        lambda *a, **k: _FakeResponse({"output": []}))
    with pytest.raises(ValueError):
        ai._call("p", "s", {"type": "object"}, 10)


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


def test_extract_phrases_feeds_reference_vocabulary_into_prompt(monkeypatch):
    captured = {}

    def call(prompt, schema_name, schema, max_tokens):
        captured["prompt"] = prompt
        return {"phrases": ["power transmission", "lt cable"]}

    monkeypatch.setattr(ai, "_call", call)
    subsidiaries = [{"subsidiary": "LASER",
                     "phrases": ["power transmission", "lt cable"]}]

    result = ai.extract_phrases("ACME", "Delhi", [{"url": "u", "text": "t"}],
                                subsidiaries)

    assert result == ["power transmission", "lt cable"]
    # Both reference phrases must reach the model so it can reuse them verbatim.
    assert "power transmission" in captured["prompt"]
    assert "lt cable" in captured["prompt"]


def test_extract_phrases_without_vocabulary_omits_the_block(monkeypatch):
    captured = {}

    def call(prompt, schema_name, schema, max_tokens):
        captured["prompt"] = prompt
        return {"phrases": ["something"]}

    monkeypatch.setattr(ai, "_call", call)
    ai.extract_phrases("ACME", "", [{"url": "u", "text": "t"}])

    assert "Subsidiary vocabulary:" not in captured["prompt"]
