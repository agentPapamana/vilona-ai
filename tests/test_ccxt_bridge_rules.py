"""One CCXT rule set for every venue (Binance, Bybit, OKX, ...).

Each test drives the bridge with a recording exchange stub so the CCXT call
shape — not a venue-specific endpoint — is what gets asserted. These are the
defects that made Binance and Bybit behave differently before the rule was
unified: a hard-coded spot market type, ``close_position`` cancelling orders
instead of closing the position, SL/TP written through a Binance-only private
endpoint, and a crypto weekend entry lock.
"""
from __future__ import annotations

import ccxt
import pytest

from config import Config
from core.quant_scanner import QuantScanner
from trading.bridge_ccxt import CCXTBridge


class _Config:
    name = "bybit"
    api_key = "k"
    api_secret = "s"
    password = ""
    sandbox = False
    rate_limit = 1000
    market_type = "swap"


class FakeExchange:
    """Records CCXT calls and returns what the bridge needs to verify them."""

    def __init__(self, symbol="BTC/USDT:USDT", sl=0.0, tp=0.0):
        self.id = "fake"
        self.symbol = symbol
        self.sl = sl
        self.tp = tp
        self.options = {}
        self.orders = []
        self.stops = []
        self.markets = {
            symbol: {
                "precision": {"price": 1, "amount": 1},
                "limits": {"amount": {"min": 0.001, "max": 100.0}},
            }
        }

    def set_sandbox_mode(self, enabled):
        self.sandbox = enabled

    def load_markets(self):
        return self.markets

    def fetch_time(self):
        return 1700000000000

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_positions(self, symbols=None):
        return [{
            "id": "7",
            "symbol": self.symbol,
            "side": "long",
            "contracts": 0.5,
            "entryPrice": 20000.0,
            "stopLoss": self.sl,
            "takeProfit": self.tp,
            "unrealizedPnl": 1.0,
        }]

    def set_trading_stop(self, symbol, params):
        self.stops.append((symbol, dict(params)))
        self.sl = float(params.get("stopLoss", self.sl) or self.sl)
        self.tp = float(params.get("takeProfit", self.tp) or self.tp)

    def create_order(self, symbol, type, side, amount, params=None):
        self.orders.append({"symbol": symbol, "type": type, "side": side,
                            "amount": amount, "params": dict(params or {})})
        return {"id": "55"}

    def fetch_balance(self):
        return {"total": {"USDT": 250.0}, "free": {"USDT": 240.0}, "USDT": {"total": 250.0, "free": 240.0}}


def _bridge(market_type="swap", exchange=None):
    bridge = CCXTBridge(_Config())
    bridge.exchange_config.market_type = market_type
    bridge._exchange = exchange or FakeExchange()
    bridge._connected = True
    return bridge


def test_market_type_is_configured_not_hardcoded(monkeypatch):
    captured = {}

    class Venue(FakeExchange):
        def __init__(self, params):
            super().__init__()
            captured.update(params)

    monkeypatch.setattr(ccxt, "bybit", Venue)
    bridge = CCXTBridge(_Config())

    assert bridge.connect() is True
    assert captured["options"]["defaultType"] == "swap"


def test_unknown_market_type_refuses_to_connect(monkeypatch):
    bridge = CCXTBridge(_Config())
    bridge.exchange_config.market_type = "futures"

    assert bridge.connect() is False
    assert bridge.is_connected() is False


def test_close_position_sends_reduce_only_market_order():
    exchange = FakeExchange()
    bridge = _bridge(exchange=exchange)

    ok, message = bridge.close_position(7, exchange.symbol)

    assert ok is True, message
    order = exchange.orders[-1]
    assert order["type"] == "market"
    assert order["side"] == "sell"  # opposite of the long position
    assert order["amount"] == 0.5
    assert order["params"]["reduceOnly"] is True


def test_close_position_on_spot_refuses_instead_of_cancelling_orders():
    exchange = FakeExchange()
    bridge = _bridge(market_type="spot", exchange=exchange)

    ok, message = bridge.close_position(7, exchange.symbol)

    assert ok is False
    assert "swap" in message
    assert exchange.orders == []


def test_modify_uses_unified_trading_stop_and_verifies_readback():
    exchange = FakeExchange()
    bridge = _bridge(exchange=exchange)

    ok, message = bridge.modify_position_sltp(7, exchange.symbol, sl=19000.0, tp=21000.0)

    assert ok is True, message
    assert exchange.stops == [(exchange.symbol, {"stopLoss": 19000.0, "takeProfit": 21000.0})]


def test_modify_reports_failure_when_broker_does_not_hold_the_stop():
    class Deaf(FakeExchange):
        def set_trading_stop(self, symbol, params):
            self.stops.append((symbol, dict(params)))  # accepted, never applied

    bridge = _bridge(exchange=Deaf())

    ok, message = bridge.modify_position_sltp(7, "BTC/USDT:USDT", sl=19000.0)

    assert ok is False
    assert "mismatch" in message


def test_entry_is_sent_without_combined_trading_stop_params():
    exchange = FakeExchange()
    bridge = _bridge(exchange=exchange)

    result = bridge.send_market_order(exchange.symbol, "buy", 0.5, sl=19000.0, tp=21000.0)

    # Bybit routes combined SL/TP to /v5/position/trading-stop, which requires an
    # existing position. The entry must go to /v5/order/create unprotected, and
    # protection is applied afterwards by modify_position_sltp().
    assert result.success is True
    assert result.ticket == 55
    assert "stopLossPrice" not in exchange.orders[0]["params"]
    assert "takeProfitPrice" not in exchange.orders[0]["params"]
    assert exchange.orders[0]["params"]["positionIdx"] == 0


def test_entry_that_cannot_be_protected_reports_failure_with_the_ticket():
    class SpotEntry(FakeExchange):
        pass

    exchange = SpotEntry()
    bridge = _bridge(market_type="spot", exchange=exchange)
    bridge._exchange.markets[exchange.symbol]["precision"] = {"price": None, "amount": 1}

    result = bridge.send_market_order(exchange.symbol, "buy", 0.5, sl=19000.0)

    assert result.success is False
    assert result.ticket == 55
    assert "protection" in result.message



def test_account_info_exposes_equity_for_fail_closed_gates():
    bridge = _bridge()

    info = bridge.get_account_info()

    assert info is not None
    assert info.equity == 250.0
    assert info.free_margin == 240.0
    assert info.currency == "USDT"


def test_account_info_is_none_when_balance_is_unusable():
    class Broke(FakeExchange):
        def fetch_balance(self):
            return {"total": {}}

    assert _bridge(exchange=Broke()).get_account_info() is None


class _VenueScanner(QuantScanner):
    def __init__(self, bridge):
        super().__init__(bridge)
        self.decisions = []
        from core.quant_core_helpers import evaluate_weekend_guard  # noqa: F401


def _weekend_cfg(name):
    cfg = Config()
    cfg.exchange.name = name
    cfg.paper_trading = True
    cfg.trading.symbols = ["BTC/USDT"]
    return cfg


class _StubBridge:
    def get_tick(self, symbol):
        from trading.bridge_ccxt import Tick
        # Saturday 2024-06-01 12:00 UTC: a forex venue is closed, crypto trades.
        return Tick(symbol=symbol, bid=60000.0, ask=60001.0,
                    timestamp_ms=1717243200000)

    def get_positions(self, symbol=None):
        return []


def _scanner(name):
    return QuantScanner(_StubBridge(), cfg=_weekend_cfg(name))


@pytest.mark.parametrize("venue", ["binance", "bybit", "okx"])
def test_crypto_venue_is_never_blocked_by_the_forex_weekend_guard(venue):
    scanner = _scanner(venue)
    feed = []
    scanner._feed = feed.append

    scanner.evaluate_symbol("BTC/USDT")

    assert not any("weekend" in line.lower() for line in feed), feed
    assert not scanner._is_forex_venue()


def test_mt5_venue_keeps_the_weekend_guard():
    scanner = _scanner("mt5")
    feed = []
    scanner._feed = feed.append

    scanner.evaluate_symbol("BTC/USDT")

    assert scanner._is_forex_venue()
    assert any("ENTRY LOCK BTC/USDT" in line for line in feed), feed
