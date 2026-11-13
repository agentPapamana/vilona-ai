from __future__ import annotations

import asyncio

from llm.client import LLMClient


def test_generate_signal_retries_failed_request_within_budget(monkeypatch):
    client = LLMClient()
    client.config.timeout = 2
    calls = []

    async def fake_request(url, payload, timeout_s=None):
        calls.append(timeout_s)
        if len(calls) == 1:
            return None
        return {"choices": [{"message": {"content": "APPROVE"}}]}

    monkeypatch.setattr(client, "_request", fake_request)
    result = asyncio.run(client.generate_signal("system", "user", retries=1))
    assert result == "APPROVE"
    assert len(calls) == 2
    assert all(value == 0.9 for value in calls)


def test_generate_signal_returns_none_after_retry_exhaustion(monkeypatch):
    client = LLMClient()
    client.config.timeout = 2
    calls = []

    async def fake_request(url, payload, timeout_s=None):
        calls.append(timeout_s)
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    result = asyncio.run(client.generate_signal("system", "user", retries=1))
    assert result is None
    assert len(calls) == 2
