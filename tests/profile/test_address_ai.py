import json

import pytest

from app.profile import address_ai


def _fake_response(data):
    return {
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(data)}]}],
    }


def test_extract_returns_fields(monkeypatch):
    payload = {"address_line1": "10 MG Road", "address_line2": "",
               "city": "Pune", "state": "Maharashtra",
               "postal_code": "411001", "country": "India",
               "evidence_url": "https://example.com"}
    class Resp:
        status_code = 200
        def json(self):
            return _fake_response(payload)
        text = ""
    monkeypatch.setattr(address_ai.requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    result = address_ai.extract_company_address("Acme", "Maharashtra", ["page text"])
    assert result == payload


def test_extract_returns_none_on_http_error(monkeypatch):
    class Resp:
        status_code = 500
        text = "boom"
        def json(self):
            return {}
    monkeypatch.setattr(address_ai.requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    assert address_ai.extract_company_address("Acme", None, ["x"]) is None


def test_extract_returns_none_on_empty_output(monkeypatch):
    class Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"output": []}
    monkeypatch.setattr(address_ai.requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    assert address_ai.extract_company_address("Acme", None, ["x"]) is None
