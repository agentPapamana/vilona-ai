"""OpenAI-compatible gateway may return SSE without an explicit stream flag."""

from __future__ import annotations

import json

from llm.client import _extract_completion_content


def test_extract_plain_openai_json_content():
    raw = json.dumps({"choices": [{"message": {"content": "{\"verdict\":\"APPROVE\"}"}}]})
    assert _extract_completion_content(raw) == '{"verdict":"APPROVE"}'


def test_extract_sse_content_chunks_and_done_marker():
    raw = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"{\\\"verdict\\\":\\\"APPRO"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"VE\\\",\\\"reason\\\":\\\"ok\\\"}"}}]}\n\n'
        'data: [DONE]\n'
    )
    assert _extract_completion_content(raw) == '{"verdict":"APPROVE","reason":"ok"}'


def test_extract_sse_message_content_when_proxy_sends_complete_chunks():
    raw = (
        'data: {"choices":[{"message":{"content":"hello"}}]}\n\n'
        'data: [DONE]\n'
    )
    assert _extract_completion_content(raw) == "hello"


def test_extract_malformed_or_empty_response_returns_empty():
    assert _extract_completion_content("") == ""
    assert _extract_completion_content("data: not-json\ndata: [DONE]") == ""
