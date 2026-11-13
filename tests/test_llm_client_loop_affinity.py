"""The judge client must survive being called from successive event loops.

The scanner is synchronous and reaches the judge through `asyncio.run(...)`,
which creates a fresh loop for every scan cycle. `aiohttp.ClientSession` is
bound to the loop that created it: caching the session in the module-level
singleton left the second and later scan cycles holding a session whose loop
was already closed, so every judge call after the first failed instantly with
`Event loop is closed` and was recorded as an ERROR.

These tests run a real HTTP server that answers with SSE chunks, then call the
client across two separate `asyncio.run()` invocations. No mocks: the second
call only succeeds if the client re-creates its session for the new loop.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from llm.client import LLMClient


class _SSEHandler(BaseHTTPRequestHandler):
    """Answers any POST with an OpenAI-style SSE stream."""

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": '{"verdict":'}}]},
            {"choices": [{"delta": {"content": ' "VETO"'}}]},
            {"choices": [{"delta": {"content": ', "reason": "ok"}'}}]},
        ]
        body = "".join(
            "data: " + json.dumps(c) + "\n\n" for c in chunks
        ) + "data: [DONE]\n\n"
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture()
def sse_server():
    server = HTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _client_for(base_url: str) -> LLMClient:
    client = LLMClient()
    client._base_url = base_url
    client.config.base_url = base_url
    client.config.provider = "openai"
    client.config.api_key = "test-key"
    client.config.timeout = 10
    return client


def test_second_call_from_a_new_event_loop_still_succeeds(sse_server):
    client = _client_for(sse_server)

    first = asyncio.run(client.generate_signal("system", "user"))
    assert first is not None, "first call must succeed"

    # A new loop: this is what the next scan cycle does. Before the fix the
    # cached session belonged to the closed loop and this returned None.
    second = asyncio.run(client.generate_signal("system", "user"))
    assert second is not None, "second call after loop teardown must succeed"
    assert "VETO" in second

    asyncio.run(client.close())


def test_repeated_cycles_never_degrade_to_errors(sse_server):
    client = _client_for(sse_server)

    results = []
    for _ in range(4):
        results.append(asyncio.run(client.generate_signal("system", "user")))

    assert all(r is not None for r in results), f"degraded at {results}"
    asyncio.run(client.close())


def test_session_is_not_shared_across_loops(sse_server):
    """The session must be re-created when the running loop changes."""
    client = _client_for(sse_server)

    asyncio.run(client.generate_signal("system", "user"))
    first_session = client._session

    asyncio.run(client.generate_signal("system", "user"))
    second_session = client._session

    assert second_session is not None
    assert second_session is not first_session, (
        "session must be recreated for the new loop; reusing it causes "
        "'Event loop is closed'"
    )

    asyncio.run(client.close())
