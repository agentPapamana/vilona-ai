from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.llm_manager import entry_decision, position_decision


class FakeClient:
    def __init__(self, response):
        self.response = response

    async def generate_signal(self, system_prompt, user_prompt):
        await asyncio.sleep(0)
        return self.response

    async def close(self):
        return None


def test_entry_manager_accepts_only_known_actions():
    signal = SimpleNamespace(symbol="XAUUSD", action="BUY", confidence=0.8, reason="trend", entry_hint=4000.0, sl_hint=None, tp_hint=None)
    risk = SimpleNamespace(approved=True, lot_size=0.01, sl_price=3990.0, tp_price=4020.0, rr_ratio=2.0, reason="ok")
    snapshot = SimpleNamespace(tick_bid=3999.9, tick_ask=4000.0, spread_pct=0.01, atr_15m=5.0, candles_15m=[], candles_1h=[], news_headlines=[], tick_value=1.0, tick_size=0.01)
    result = entry_decision(FakeClient('{"action":"ADJUST","reason":"tighten","lot_size":0.005,"sl_price":3995,"tp_price":4020}'), signal, risk, snapshot, {"equity":1000}, 1)
    assert result["action"] == "ADJUST"


def test_invalid_manager_response_is_none():
    signal = SimpleNamespace(symbol="XAUUSD", action="BUY", confidence=0.8, reason="trend", entry_hint=4000.0, sl_hint=None, tp_hint=None)
    risk = SimpleNamespace(approved=True, lot_size=0.01, sl_price=3990.0, tp_price=4020.0, rr_ratio=2.0, reason="ok")
    snapshot = SimpleNamespace(tick_bid=3999.9, tick_ask=4000.0, spread_pct=0.01, atr_15m=5.0, candles_15m=[], candles_1h=[], news_headlines=[], tick_value=1.0, tick_size=0.01)
    assert entry_decision(FakeClient("not json"), signal, risk, snapshot, {}, 1) is None


def test_position_manager_accepts_close():
    position = SimpleNamespace(ticket=1, symbol="XAUUSD", type="buy", volume=0.01, price_open=4000, price_current=4005, sl=3990, tp=4020, profit=1)
    snapshot = SimpleNamespace(symbol="XAUUSD", tick_bid=4005, tick_ask=4005.2, spread_pct=0.005, atr_15m=5)
    result = position_decision(FakeClient('{"action":"CLOSE","reason":"reversal"}'), position, snapshot, 1)
    assert result["action"] == "CLOSE"
