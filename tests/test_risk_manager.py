"""Risk manager sizing/cap tests."""
from __future__ import annotations

from dataclasses import dataclass

from agents import MarketSnapshot, Signal
from agents.risk_manager import evaluate as risk_evaluate
from config import TradingConfig


def _snapshot(symbol: str, bid: float, atr: float, tick_value: float, tick_size: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        tick_bid=bid,
        tick_ask=bid,
        candles_15m=[],
        candles_1h=[],
        spread_pct=0.0001,
        atr_15m=atr,
        news_headlines=[],
        tick_value=tick_value,
        tick_size=tick_size,
    )


def test_cap_margin_respects_free_margin():
    """Lot size must be capped so one position cannot consume most free margin."""
    snapshot = _snapshot("EURUSD", 1.12176, 0.000161, tick_value=1.0, tick_size=1e-05)
    signal = Signal(symbol="EURUSD", action="BUY", confidence=0.9, reason="uptrend")

    verdict = risk_evaluate(
        signal=signal,
        snapshot=snapshot,
        equity_usd=10000.0,
        cfg=TradingConfig(),
        free_margin=20000.0,
        open_positions=[],
    )

    assert verdict.lot_size <= 0.10, verdict.reason
    assert verdict.lot_size > 0, verdict.reason


def test_cap_notional_rejects_when_exposure_exceeded():
    """New order must be rejected when total notional already exceeds 2x equity."""
    @dataclass
    class P:
        symbol: str
        volume: float

    snapshot = _snapshot("USDJPY", 158.205, 0.019357, tick_value=0.632191, tick_size=0.001)
    signal = Signal(symbol="USDJPY", action="BUY", confidence=0.9, reason="trend")

    verdict = risk_evaluate(
        signal=signal,
        snapshot=snapshot,
        equity_usd=10000.0,
        cfg=TradingConfig(),
        free_margin=1000.0,
        open_positions=[P("EURUSD", 45.0), P("GBPUSD", 47.0)],
    )

    assert verdict.approved is False
    assert "notional" in verdict.reason.lower() or "lot=0" in verdict.reason.lower()


def test_cap_notional_allows_small_exposure():
    """A clean account with no positions must still approve a normal order."""
    snapshot = _snapshot("EURUSD", 1.12176, 0.000161, tick_value=1.0, tick_size=1e-05)
    signal = Signal(symbol="EURUSD", action="BUY", confidence=0.9, reason="uptrend")

    verdict = risk_evaluate(
        signal=signal,
        snapshot=snapshot,
        equity_usd=100000.0,
        cfg=TradingConfig(),
        free_margin=100000.0,
        open_positions=[],
    )

    assert verdict.approved is True, verdict.reason
    assert verdict.lot_size > 0
