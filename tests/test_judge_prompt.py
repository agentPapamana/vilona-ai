"""Judge payload completeness: the model must see the numbers it needs.

A judge that only sees symbol/action/confidence/sl/tp has no basis to reason and
rejects everything. These tests pin the context fields that must reach the model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from agents import MarketSnapshot, Signal
from agents.judge import build_user_prompt
from agents.risk_manager import RiskVerdict


def make_signal() -> Signal:
    return Signal(
        symbol="XAUUSD",
        action="BUY",
        confidence=0.85,
        reason="H1 uptrend pullback to EMA21",
        entry_hint=4037.8,
        source_role="trader",
    )


def make_risk() -> RiskVerdict:
    return RiskVerdict(
        approved=True,
        lot_size=0.01,
        sl_price=4033.0,
        tp_price=4049.0,
        rr_ratio=2.13,
        reason="sized at 1% risk",
    )


def make_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="XAUUSD",
        tick_bid=4037.6,
        tick_ask=4037.8,
        candles_15m=[],
        candles_1h=[],
        spread_pct=0.0050,
        atr_15m=3.4,
        news_headlines=[],
        tick_value=1.0,
        tick_size=0.01,
    )


def test_prompt_includes_market_context():
    payload = json.loads(
        build_user_prompt(make_signal(), make_risk(), snapshot=make_snapshot())
    )
    ctx = payload["market_context"]
    assert ctx["spread_pct"] == 0.0050
    assert ctx["atr_15m"] == 3.4
    assert ctx["bid"] == 4037.6
    assert ctx["ask"] == 4037.8
    assert ctx["timeframe"] == "15m/1h"


def test_prompt_includes_account_context():
    payload = json.loads(
        build_user_prompt(
            make_signal(),
            make_risk(),
            snapshot=make_snapshot(),
            account={"equity": 99137.39, "free_margin": 99097.54, "open_positions": 0},
        )
    )
    acc = payload["account"]
    assert acc["equity"] == 99137.39
    assert acc["free_margin"] == 99097.54
    assert acc["open_positions"] == 0


def test_prompt_includes_risk_as_pct_of_equity():
    payload = json.loads(
        build_user_prompt(
            make_signal(),
            make_risk(),
            snapshot=make_snapshot(),
            account={"equity": 100000.0, "free_margin": 100000.0, "open_positions": 0},
        )
    )
    # SL distance 4.8 on 0.01 lot with tick_value 1.0 => ~$0.05 risk => tiny %
    assert "risk_pct_of_equity" in payload["risk_verdict"]
    assert payload["risk_verdict"]["risk_pct_of_equity"] >= 0.0


def test_prompt_still_works_without_optional_context():
    payload = json.loads(build_user_prompt(make_signal(), make_risk()))
    assert payload["signal"]["symbol"] == "XAUUSD"
    assert "market_context" not in payload or payload["market_context"] is None
