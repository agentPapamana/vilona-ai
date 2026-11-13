"""Ensure timed-out judge requests close aiohttp sessions cleanly."""
import asyncio

import pytest

import core.llm_gate as gate
from agents import Signal
from agents.risk_manager import RiskVerdict
from llm.client import LLMClient


class _SlowClient(LLMClient):
    async def generate_signal(self, system_prompt, user_prompt, **kwargs):
        await self._get_session()
        await asyncio.sleep(10)
        return '{"verdict":"VETO","reason":"late"}'


def test_timeout_closes_client_session(monkeypatch):
    client = _SlowClient()
    client.config.timeout = 30
    signal = Signal(symbol="EURUSD", action="BUY", confidence=0.9, reason="test")
    risk = RiskVerdict(approved=True, reason="ok", lot_size=0.01, sl_price=1.0, tp_price=1.1)

    approved, reason = gate.llm_judge_verdict(
        client, signal, risk, timeout_s=0.01
    )

    assert not approved
    assert "ERROR:" in reason
    assert client._session is None


def test_close_after_loop_shutdown_detaches_session():
    client = LLMClient()

    async def create_session():
        await client._get_session()

    asyncio.run(create_session())
    # Loop has now shut down; cleanup must detach rather than leak the session.
    asyncio.run(client.close())
    assert client._session is None
