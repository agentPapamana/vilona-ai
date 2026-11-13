import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from llm.client import LLMClient


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self._body


class FakeSession:
    closed = False
    _loop = None

    def __init__(self, response):
        self.response = response

    def post(self, *args, **kwargs):
        return self.response


def client_with_response(status, body, headers=None):
    client = LLMClient.__new__(LLMClient)
    client.config = SimpleNamespace(
        api_key="never-log-this", provider="openai", model="test-model",
        max_tokens=12, temperature=0.1, timeout=10,
    )
    return client


def fake_session(response):
    session = FakeSession(response)
    return session


def run_with_response(client, response):
    async def get_session():
        return fake_session(response)
    client._get_session = get_session
    return client


def test_non_200_logs_status_request_id_and_timing_not_response_secrets(caplog):
    client = client_with_response(503, "provider secret error body", {"x-request-id": "rid-123"})
    run_with_response(client, FakeResponse(503, "provider secret error body", {"x-request-id": "rid-123"}))
    caplog.set_level(logging.INFO)
    result = asyncio.run(client._request("https://example.invalid", {}))
    assert result is None
    output = caplog.text
    assert "503" in output and "rid-123" in output and "headers" in output
    assert "provider secret error body" not in output
    assert "never-log-this" not in output


def test_empty_and_malformed_responses_are_distinguished(caplog):
    caplog.set_level(logging.WARNING)
    empty = run_with_response(client_with_response(200, ""), FakeResponse(200, ""))
    malformed = run_with_response(client_with_response(200, "not-json"), FakeResponse(200, "not-json"))
    assert asyncio.run(empty._request("https://example.invalid", {})) is None
    assert asyncio.run(malformed._request("https://example.invalid", {})) is None
    assert "body_chars=0" in caplog.text
    assert "body_chars=8" in caplog.text


def test_non_object_json_is_reported_as_malformed(caplog):
    caplog.set_level(logging.WARNING)
    client = run_with_response(client_with_response(200, json.dumps([1, 2])), FakeResponse(200, json.dumps([1, 2])))
    assert asyncio.run(client._request("https://example.invalid", {})) is None
    assert "malformed kind=list" in caplog.text


def test_success_logs_body_duration_and_returns_json(caplog):
    caplog.set_level(logging.INFO)
    expected = {"choices": [{"message": {"content": "ok"}}]}
    client = run_with_response(client_with_response(200, json.dumps(expected), {"request-id": "rid-ok"}), FakeResponse(200, json.dumps(expected), {"request-id": "rid-ok"}))
    assert asyncio.run(client._request("https://example.invalid", {})) == expected
    assert "body received" in caplog.text
    assert "headers took" in caplog.text


def test_sse_content_is_parsed_and_diagnosed(caplog):
    caplog.set_level(logging.INFO)
    body = 'data: {"choices":[{"delta":{"content":"ok"}}]}\ndata: [DONE]\n'
    client = run_with_response(client_with_response(200, body), FakeResponse(200, body))
    result = asyncio.run(client._request("https://example.invalid", {}))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert "SSE content parsed" in caplog.text
