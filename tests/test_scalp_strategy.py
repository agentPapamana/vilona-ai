"""Scalp 15m/5m strategy: signal selection and configuration wiring."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import Config, TradingConfig
from core.quant_scanner import compute_scalp_5m_signal


def _candles(closes, step=300):
    rows = []
    for i, close in enumerate(closes):
        rows.append({
            "time": 1700000000 + i * step,
            "open": close - 0.05,
            "high": close + 0.30,
            "low": close - 0.30,
            "close": close,
            "volume": 10.0,
        })
    return rows


def test_scalp_emits_buy_on_15m_uptrend_with_5m_pullback():
    trend = _candles([100.0 + i * 0.20 for i in range(60)], step=900)
    trigger = _candles([112.0 + i * 0.02 for i in range(50)] + [113.0 - i * 0.04 for i in range(7)] + [112.75 + i * 0.05 for i in range(3)])
    price = float(trigger[-1]["close"]) + 0.05

    action, confidence, reason, atr = compute_scalp_5m_signal(
        trend, trigger, price, band_mult=1.5
    )

    assert action == "BUY", reason
    assert atr > 0
    assert confidence > 0


def test_scalp_emits_sell_on_15m_downtrend_with_5m_pullback():
    trend = _candles([100.0 - i * 0.20 for i in range(60)], step=900)
    trigger = _candles([88.0 - i * 0.02 for i in range(50)] + [87.0 + i * 0.04 for i in range(7)] + [87.25 - i * 0.05 for i in range(3)])
    price = float(trigger[-1]["close"]) - 0.05

    action, _, reason, atr = compute_scalp_5m_signal(trend, trigger, price, band_mult=1.5)

    assert action == "SELL", reason
    assert atr > 0


def test_scalp_waits_when_price_is_far_from_trigger_ema():
    trend = _candles([100.0 + i * 0.20 for i in range(60)], step=900)
    trigger = _candles([112.0 + i * 0.02 for i in range(60)])
    price = float(trigger[-1]["close"]) + 50.0

    action, confidence, _, _ = compute_scalp_5m_signal(trend, trigger, price, band_mult=1.5)

    assert action == "WAIT"
    assert confidence == 0.0


def test_scalp_waits_on_flat_trend():
    trend = _candles([100.0 + (i % 2) * 0.01 for i in range(60)], step=900)
    trigger = _candles([100.0 + (i % 2) * 0.01 for i in range(60)])
    price = float(trigger[-1]["close"])

    action, _, _, _ = compute_scalp_5m_signal(trend, trigger, price, band_mult=1.5)

    assert action == "WAIT"


def test_scalp_waits_with_insufficient_candles():
    action, confidence, reason, atr = compute_scalp_5m_signal(
        _candles([100.0] * 10), _candles([100.0] * 10), 100.0, band_mult=1.5
    )

    assert action == "WAIT"
    assert confidence == 0.0
    assert atr == 0.0

def test_breakout_signal_requires_aligned_1h_trend():
    from core.quant_scanner import compute_breakout_signal
    trend = _candles([100.0 + i * 0.2 for i in range(60)], step=3600)
    candles = _candles([100.0 + i * 0.05 for i in range(25)], step=900)
    candles[-1]["close"] = 102.0
    candles[-1]["high"] = 102.0
    action, confidence, reason, atr = compute_breakout_signal(candles, trend, 102.0)
    assert action == "BUY", reason
    assert confidence >= 0.55
    assert atr > 0


def test_breakout_signal_waits_without_range_break():
    from core.quant_scanner import compute_breakout_signal
    trend = _candles([100.0 + i * 0.2 for i in range(60)], step=3600)
    candles = _candles([100.0 + i * 0.05 for i in range(25)], step=900)
    action, confidence, reason, _ = compute_breakout_signal(candles, trend, candles[-1]["close"])
    assert action == "WAIT", reason
    assert confidence == 0.0


def test_strategy_defaults_to_pullback():
    assert TradingConfig().strategy == "pullback"
    assert TradingConfig().scalp_pullback_band == 1.5


def test_strategy_env_override(monkeypatch):
    monkeypatch.setenv("KINGAI_STRATEGY", "SCALP_5M")
    monkeypatch.setenv("KINGAI_SCALP_PULLBACK_BAND", "2.0")

    cfg = Config.from_env()

    assert cfg.trading.strategy == "scalp_5m"
    assert cfg.trading.scalp_pullback_band == 2.0


class _Bridge:
    """Minimal bridge that serves 15m/5m scalp candles and records orders."""

    def __init__(self, trend_step: float, trigger_step: float):
        self.trend_step = trend_step
        self.trigger_step = trigger_step
        self.orders: list[dict] = []
        self.positions: list = []

    def get_positions(self, symbol=None):
        return list(self.positions)

    def get_tick(self, symbol):
        price = 100.0 + self.trigger_step * 59
        return SimpleNamespace(symbol=symbol, bid=price, ask=price + 0.02,
                               timestamp_ms=int(__import__("time").time() * 1000))

    def get_candles(self, symbol, timeframe, limit=100):
        step = 3600 if timeframe == "1h" else (900 if timeframe == "15m" else 300)
        slope = self.trend_step if timeframe in ("1h", "15m") else self.trigger_step
        if timeframe == "5m":
            closes = [100.0 + i * slope for i in range(limit - 10)] + \
                     [100.0 + (limit - 10) * slope - i * 0.02 for i in range(7)] + \
                     [100.0 + (limit - 10) * slope - 0.14 + i * 0.03 for i in range(3)]
            candles = _candles(closes, step=step)
            for candle in candles:
                candle["high"] += 8.0
                candle["low"] -= 8.0
        else:
            candles = _candles([100.0 + i * slope for i in range(limit)], step=step)
        return candles

    def get_symbol_info(self, symbol):
        from trading.bridge_base import SymbolInfo
        return SymbolInfo(symbol=symbol, point=0.01, digits=2, lot_step=0.01,
                          lot_min=0.01, lot_max=100.0, tick_value=1.0, tick_size=0.01)

    def get_server_day_start(self, now_epoch):
        return 1700000000

    def get_history_deals(self, since_epoch):
        return []

    def get_account_info(self):
        return SimpleNamespace(equity=100000.0, free_margin=100000.0, balance=100000.0)

    def send_market_order(self, symbol, action, volume, sl=None, tp=None, comment="", magic=0):
        self.orders.append({"symbol": symbol, "action": action, "volume": volume, "sl": sl})
        # Mirror the fill so the post-fill SL verification reads back a protected position.
        self.positions.append(SimpleNamespace(
            ticket=1, symbol=symbol, type=action, volume=volume,
            price_open=100.0, price_current=100.0, sl=sl or 0.0, tp=tp or 0.0,
        ))
        return SimpleNamespace(success=True, ticket=1, message="ok")

    def modify_position_sltp(self, ticket, symbol, sl=None, tp=None):
        for position in self.positions:
            if position.ticket == ticket:
                position.sl = sl or 0.0
                position.tp = tp or 0.0
        return SimpleNamespace(success=True, message="ok")

    def close_position(self, ticket, symbol, volume=None, comment="", magic=0):
        self.positions = [p for p in self.positions if p.ticket != ticket]
        return SimpleNamespace(success=True, message="ok")


def _build_scanner(tmp_path, monkeypatch, strategy):
    from core import state_db as state_db_mod
    from core.quant_scanner import QuantScanner

    db_path = str(tmp_path / f"{strategy}.db")
    monkeypatch.setattr(state_db_mod, "get_config", lambda: SimpleNamespace(
        database=SimpleNamespace(path=db_path)
    ))
    cfg = SimpleNamespace(trading=TradingConfig(
        symbols=["XAUUSD"], max_concurrent_positions=1, max_positions_per_symbol=1,
        min_confidence=0.0, symbol_cooldown_seconds=0, strategy=strategy,
        max_lot=0.01, min_lot=0.01, max_risk_per_trade_pct=0.5,
    ))
    # Keep the synthetic 15m trend clearly above the production 0.35 ATR floor.
    bridge = _Bridge(trend_step=0.80, trigger_step=0.02)
    return QuantScanner(bridge=bridge, cfg=cfg, dashboard=None), bridge


def test_scanner_scalp_mode_places_order_from_5m_path(tmp_path, monkeypatch):
    scanner, bridge = _build_scanner(tmp_path, monkeypatch, "scalp_5m")

    scanner.evaluate_symbol("XAUUSD")

    assert len(bridge.orders) == 1, "scalp_5m must reach order submission"
    assert bridge.orders[0]["action"].upper() == "BUY"
    assert bridge.orders[0]["volume"] <= 0.01

def test_scanner_breakout_mode_uses_protected_order_path(tmp_path, monkeypatch):
    from core import quant_scanner
    scanner, bridge = _build_scanner(tmp_path, monkeypatch, "breakout")
    scanner.cfg.trading.min_stop_distance_by_symbol = {"XAUUSD": 0.0}
    monkeypatch.setattr(
        quant_scanner,
        "compute_breakout_signal",
        lambda *args, **kwargs: ("BUY", 0.70, "fixture breakout", 8.0),
    )
    scanner.evaluate_symbol("XAUUSD")
    assert len(bridge.orders) == 1, "breakout must reach order submission"
    assert bridge.orders[0]["action"].upper() == "BUY"
    assert bridge.orders[0]["volume"] <= 0.01


def test_scanner_pullback_mode_uses_original_rules(tmp_path, monkeypatch):
    scanner, bridge = _build_scanner(tmp_path, monkeypatch, "pullback")

    scanner.evaluate_symbol("XAUUSD")

    # 1H candles are not rising in this fixture, so the original pullback path waits.
    assert bridge.orders == [], "pullback mode must not trade on 5m scalp conditions"


def test_per_symbol_cap_env_override(monkeypatch):
    monkeypatch.setenv("KINGAI_MAX_POSITIONS_PER_SYMBOL", "100")
    monkeypatch.setenv("KINGAI_SYMBOL_COOLDOWN_SECONDS", "0")

    cfg = Config.from_env()

    assert cfg.trading.max_positions_per_symbol == 100
    assert cfg.trading.symbol_cooldown_seconds == 0
    assert not [e for e in cfg.validate() if "max_positions_per_symbol" in e], "100 is the allowed ceiling"


@pytest.mark.parametrize("value", ["0", "101"])
def test_per_symbol_cap_rejects_out_of_range(monkeypatch, value):
    monkeypatch.setenv("KINGAI_MAX_POSITIONS_PER_SYMBOL", value)

    with pytest.raises(ValueError, match="KINGAI_MAX_POSITIONS_PER_SYMBOL"):
        Config.from_env()


def test_scalp_waits_when_1h_trend_opposes_15m_trend():
    """When 15m is bullish but 1h is in a bear downtrend, refuse to buy."""
    trend_15m = _candles([100.0 + i * 0.20 for i in range(60)], step=900)
    trend_1h_bear = _candles([120.0 - i * 0.30 for i in range(60)], step=3600)
    trigger = _candles([112.0 + i * 0.02 for i in range(50)] + [113.0 - i * 0.04 for i in range(7)] + [112.75 + i * 0.05 for i in range(3)])
    price = float(trigger[-1]["close"]) + 0.05

    action, conf, reason, atr = compute_scalp_5m_signal(trend_15m, trigger, price, band_mult=1.5, c1h=trend_1h_bear)

    assert action == "WAIT"
    assert "1h trend bearish" in reason
    assert conf == 0.0


def test_scalp_waits_when_5m_rsi_overbought():
    """When 5m RSI is overbought (> 70), refuse to chase the top."""
    trend = _candles([100.0 + i * 0.20 for i in range(60)], step=900)
    # Strictly monotonic up moves produce RSI = 100
    trigger_overbought = _candles([112.0 + i * 0.05 for i in range(60)])
    price = float(trigger_overbought[-1]["close"]) + 0.05

    action, conf, reason, atr = compute_scalp_5m_signal(trend, trigger_overbought, price, band_mult=1.5)

    assert action == "WAIT"
    assert "RSI overbought" in reason


def test_scalp_waits_when_5m_rsi_oversold():
    """When 5m RSI is oversold (< 30), refuse to sell into the flush."""
    trend = _candles([100.0 - i * 0.20 for i in range(60)], step=900)
    # Strictly monotonic down moves produce RSI = 0
    trigger_oversold = _candles([88.0 - i * 0.05 for i in range(60)])
    price = float(trigger_oversold[-1]["close"]) - 0.05

    action, conf, reason, atr = compute_scalp_5m_signal(trend, trigger_oversold, price, band_mult=1.5)

    assert action == "WAIT"
    assert "RSI oversold" in reason


def _valid_buy_inputs():
    trend = _candles([100.0 + i * 0.20 for i in range(60)], step=900)
    trigger = _candles([112.0 + i * 0.02 for i in range(50)] + [113.0 - i * 0.04 for i in range(7)] + [112.75 + i * 0.05 for i in range(3)])
    return trend, trigger, float(trigger[-1]["close"]) + 0.05


def _valid_sell_inputs():
    trend = _candles([100.0 - i * 0.20 for i in range(60)], step=900)
    trigger = _candles([88.0 - i * 0.02 for i in range(50)] + [87.0 + i * 0.04 for i in range(7)] + [87.25 - i * 0.05 for i in range(3)])
    return trend, trigger, float(trigger[-1]["close"]) - 0.05


def test_scalp_waits_when_spread_is_too_costly_relative_to_atr():
    """A spread that eats a large share of ATR must block the entry."""
    trend, trigger, price = _valid_buy_inputs()

    action, conf, reason, atr = compute_scalp_5m_signal(
        trend, trigger, price, band_mult=1.5, spread_price=99.0, max_spread_atr=0.35
    )

    assert action == "WAIT"
    assert "spread too costly" in reason
    assert conf == 0.0


def test_scalp_allows_entry_when_spread_is_cheap_relative_to_atr():
    """A tight spread must not block an otherwise valid setup."""
    trend, trigger, price = _valid_buy_inputs()
    _, _, _, atr = compute_scalp_5m_signal(trend, trigger, price, band_mult=1.5)

    action, _, reason, _ = compute_scalp_5m_signal(
        trend, trigger, price, band_mult=1.5, spread_price=0.1 * atr, max_spread_atr=0.35
    )

    assert action == "BUY", reason


def test_scalp_waits_when_last_5m_candle_body_opposes_trend():
    """A material counter-trend 5m close must cancel a bullish setup."""
    trend, trigger, _ = _valid_buy_inputs()
    reversing = _candles([112.0 + i * 0.02 for i in range(50)] + [113.0 - i * 0.04 for i in range(7)] + [112.75 + i * 0.05 for i in range(2)])
    last = trigger[-1]
    reversing.append({**last, "open": float(last["close"]) + 3.0, "close": float(last["close"]), "high": float(last["close"]) + 3.2})
    price = float(reversing[-1]["close"])

    action, conf, reason, atr = compute_scalp_5m_signal(trend, reversing, price, band_mult=1.5)

    assert action == "WAIT"
    assert "candle close opposes trend" in reason


def test_scalp_waits_when_15m_ema21_slope_turns_down():
    """A bullish EMA21 > EMA50 without a rising slope must not produce BUY."""
    falling = [100.0 + i * 0.20 for i in range(55)] + [110.6 - i * 0.55 for i in range(5)]
    trend = _candles(falling, step=900)
    _, trigger, price = _valid_buy_inputs()

    action, conf, reason, atr = compute_scalp_5m_signal(trend, trigger, price, band_mult=1.5)

    assert action == "WAIT"
    assert "slope does not confirm" in reason


def test_scalp_sell_requires_rising_spread_free_slope():
    """Sell setups pass the same cost and confirmation gates as buys."""
    trend, trigger, price = _valid_sell_inputs()
    _, _, _, atr = compute_scalp_5m_signal(trend, trigger, price, band_mult=1.5)

    action, _, reason, _ = compute_scalp_5m_signal(
        trend, trigger, price, band_mult=1.5, spread_price=0.1 * atr, max_spread_atr=0.35
    )

    assert action == "SELL", reason
