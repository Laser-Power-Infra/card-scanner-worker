import json

from app.profile import address_ai


def _address(**overrides):
    base = {
        "address_line1": "10 MG Road",
        "address_line2": "",
        "city": "Pune",
        "state": "Maharashtra",
        "postal_code": "411001",
        "country": "India",
        "address_type": "head_office",
        "evidence_url": "https://example.com",
        "quote": "Registered office 10 MG Road Pune 411001",
    }
    base.update(overrides)
    return base


def _fake_response(data):
    return {
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(data)}]}],
    }


def test_extract_returns_address_list(monkeypatch):
    payload = {"addresses": [_address(),
                             _address(address_type="branch",
                                      address_line1="5 Station Road")]}
    class Resp:
        status_code = 200
        def json(self):
            return _fake_response(payload)
        text = ""
    monkeypatch.setattr(address_ai.requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    result = address_ai.extract_company_addresses("Acme", "Maharashtra", ["page"])
    assert len(result) == 2
    assert result[0]["address_type"] == "head_office"
    assert result[1]["address_type"] == "branch"


def test_extract_returns_empty_on_http_error(monkeypatch):
    class Resp:
        status_code = 500
        text = "boom"
        def json(self):
            return {}
    monkeypatch.setattr(address_ai.requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    assert address_ai.extract_company_addresses("Acme", None, ["x"]) == []


def test_extract_returns_empty_on_empty_output(monkeypatch):
    class Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"output": []}
    monkeypatch.setattr(address_ai.requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    assert address_ai.extract_company_addresses("Acme", None, ["x"]) == []


def test_strip_po_box_segments():
    cleaned = address_ai.strip_po_box({
        "address_line1": "P.O. Box 123, 10 MG Road",
        "address_line2": "P.B. No. 1568, Shivaji Nagar",
    })
    assert cleaned["address_line1"] == "10 MG Road"
    assert cleaned["address_line2"] == "Shivaji Nagar"


def test_strip_po_box_whole_line():
    cleaned = address_ai.strip_po_box({
        "address_line1": "5-1-503 Ranigunj",
        "address_line2": "P.B. No. 1568",
    })
    assert cleaned["address_line1"] == "5-1-503 Ranigunj"
    assert cleaned["address_line2"] == ""


def test_temperature_zero_and_retry_without_temperature(monkeypatch):
    payloads = []

    class Resp:
        def __init__(self, status, text, body=None):
            self.status_code = status
            self.text = text
            self._body = body or {}

        def json(self):
            return self._body

    def fake_post(url, headers=None, json=None, timeout=None):
        payloads.append(dict(json))
        if len(payloads) == 1:
            return Resp(400, "temperature is not supported")
        return Resp(200, "", _fake_response({"addresses": [_address()]}))

    monkeypatch.setattr(address_ai.requests, "post", fake_post)
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(address_ai.config, "ADDRESS_TEMPERATURE", "0")
    result = address_ai.extract_company_addresses("Acme", "Maharashtra", ["page"])
    assert result
    assert payloads[0]["temperature"] == 0
    assert "temperature" not in payloads[1]


def test_permanent_4xx_is_not_retried(monkeypatch):
    calls = []

    class Resp:
        status_code = 400
        text = "bad request"

        def json(self):
            return {}

    def fake_post(*args, **kwargs):
        calls.append(1)
        return Resp()

    monkeypatch.setattr(address_ai.requests, "post", fake_post)
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    assert address_ai.extract_company_addresses("Acme", None, ["page"]) == []
    assert len(calls) == 1


def test_loads_json_repairs_unescaped_inner_quotes():
    raw = ('{"addresses": [{"address_line1": "Shed "A", Sai Dham", '
           '"quote": "Shed "A", Sai Dham Complex"}]}')
    data = address_ai._loads_json(raw)
    assert data["addresses"][0]["address_line1"] == 'Shed "A", Sai Dham'
    assert data["addresses"][0]["quote"] == 'Shed "A", Sai Dham Complex'


def test_loads_json_repairs_raw_newlines_in_strings():
    raw = '{"a": "line one\nline two", "b": 1}'
    parsed = address_ai._loads_json(raw)
    assert parsed["a"] == "line one\nline two"
    assert parsed["b"] == 1


def test_loads_json_leaves_valid_json_alone():
    raw = '{"addresses": [{"address_line1": "10 MG Road"}]}'
    assert address_ai._loads_json(raw)["addresses"][0]["address_line1"] == "10 MG Road"


def test_extract_recovers_address_with_quoted_text(monkeypatch):
    body = ('{"addresses": [{"address_line1": "Shed "A", Sai Dham", '
            '"address_line2": "", "city": "Pune", '
            '"state": "Maharashtra", "postal_code": "411001", '
            '"country": "India", "address_type": "other", '
            '"evidence_url": "https://example.com", "quote": "x"}]}')

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"output": [{"type": "message", "content": [
                {"type": "output_text", "text": body}]}]}

    monkeypatch.setattr(address_ai.requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(address_ai.config, "OPENAI_API_KEY", "test-key")
    result = address_ai.extract_company_addresses("Acme", None, ["page"])
    assert len(result) == 1
    assert result[0]["address_line1"] == 'Shed "A", Sai Dham'
