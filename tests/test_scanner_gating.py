"""Scanner gating: global position cap, per-symbol cooldown, order persistence."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import pytest

from config import TradingConfig
from core.quant_scanner import QuantScanner


@dataclass
class FakePos:
    ticket: int
    symbol: str
    type: str = "buy"
    volume: float = 0.01
    price_open: float = 1.1000
    price_current: float = 1.1010
    sl: float = 1.0950
    tp: float = 1.1100


@dataclass
class FakeTick:
    symbol: str
    bid: float
    ask: float
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class FakeResult:
    success: bool = True
    message: str = "ok"
    ticket: int = 0


class FakeBridge:
    """Minimal bridge: controllable positions, always-fresh candles, one signal."""

    def __init__(self, positions=None):
        self.positions = list(positions or [])
        self.orders_placed: list[dict] = []
        self.modified: list[tuple] = []
        self._next_ticket = 9000

    def get_positions(self, symbol: Optional[str] = None):
        if symbol:
            return [p for p in self.positions if p.symbol == symbol]
        return list(self.positions)

    def get_tick(self, symbol: str):
        return FakeTick(symbol, 1.1010, 1.1012)

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100):
        # Uptrend in EURUSD range with a tight pullback so the scanner emits a BUY.
        rows = []
        for i in range(limit):
            base = 1.0880 + i * 0.00018
            rows.append({
                "time": 1700000000 + i * 900,
                "open": base,
                "high": base + 0.00060,
                "low": base - 0.00060,
                "close": base + 0.00052,
                "volume": 10.0,
            })
        rows[-1]["close"] = rows[-1]["open"] + 0.00002  # pull back toward EMA21
        return rows

    def get_symbol_info(self, symbol: str):
        from trading.bridge_base import SymbolInfo
        return SymbolInfo(
            symbol=symbol, point=0.00001, digits=5,
            lot_step=0.01, lot_min=0.01, lot_max=100.0,
            tick_value=1.0, tick_size=0.00001,
        )

    def get_server_day_start(self, now_epoch):
        return 1700000000

    def get_history_deals(self, since_epoch):
        return []

    def get_account_info(self):
        return type('AccountInfo', (), {
            'free_margin': 100000.0,
            'equity': 100000.0
        })()

    def send_market_order(self, symbol, action, volume, sl=None, tp=None, comment="", magic=0):
        self._next_ticket += 1
        self.orders_placed.append({
            "symbol": symbol, "action": action, "volume": volume, "sl": sl, "tp": tp,
            "magic": magic,
        })
        # Model the fill so post-fill SL verification sees a protected position.
        self.positions.append(FakePos(
            ticket=self._next_ticket, symbol=symbol, volume=volume,
            sl=(sl or 0.0), tp=(tp or 0.0),
        ))
        return FakeResult(success=True, ticket=self._next_ticket)

    def modify_position_sltp(self, ticket, symbol, sl=None, tp=None):
        self.modified.append((ticket, sl, tp))
        for p in self.positions:
            if p.ticket == ticket:
                p.sl = sl or 0.0
                p.tp = tp or 0.0
        return FakeResult(success=True)

    def close_position(self, ticket, symbol, volume=None, comment="", magic=0):
        self.closed = getattr(self, "closed", [])
        self.closed.append(ticket)
        self.positions = [p for p in self.positions if p.ticket != ticket]
        return FakeResult(success=True)


@pytest.fixture()
def scanner(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core import state_db as state_db_mod

    db_path = str(tmp_path / "scanner_test.db")
    monkeypatch.setattr(state_db_mod, "get_config", lambda: SimpleNamespace(
        database=SimpleNamespace(path=db_path)
    ))

    cfg = SimpleNamespace(
        trading=TradingConfig(
            symbols=["EURUSD"],
            max_concurrent_positions=3,
            max_positions_per_symbol=1,
            min_confidence=0.0,
            symbol_cooldown_seconds=300,
        )
    )
    sc = QuantScanner(bridge=FakeBridge(), cfg=cfg, dashboard=None)
    return sc


def test_cap_counts_all_positions_not_just_symbol(scanner):
    """Two EURUSD + two GBPUSD must hit a global cap of 3 even though no symbol has 3."""
    bridge = FakeBridge([
        FakePos(1, "EURUSD"), FakePos(2, "EURUSD"),
        FakePos(3, "GBPUSD"), FakePos(4, "GBPUSD"),
    ])
    scanner.bridge = bridge

    scanner.evaluate_symbol("EURUSD")

    assert bridge.orders_placed == [], "global cap of 3 must block a 5th position"


def test_cap_allows_order_under_global_limit(scanner):
    bridge = FakeBridge([FakePos(1, "EURUSD"), FakePos(2, "GBPUSD")])
    scanner.bridge = bridge

    # USDJPY has no existing position; global book has room and symbol cap has room.
    scanner.evaluate_symbol("USDJPY")

    assert len(bridge.orders_placed) == 1, "2 < 3 open positions, order must go through"


def test_per_symbol_cap_blocks_stacked_entry(scanner):
    """One existing EURUSD blocks another even while global capacity remains."""
    bridge = FakeBridge([FakePos(1, "EURUSD")])
    scanner.bridge = bridge

    scanner.evaluate_symbol("EURUSD")

    assert bridge.orders_placed == [], "max_positions_per_symbol=1 must block stacking"


def test_per_symbol_cap_does_not_block_other_symbol(scanner):
    """The cap applies to the requested symbol, not the entire book."""
    bridge = FakeBridge([FakePos(1, "EURUSD")])
    scanner.bridge = bridge

    scanner.evaluate_symbol("GBPUSD")

    assert len(bridge.orders_placed) == 1
    assert bridge.orders_placed[0]["symbol"] == "GBPUSD"


def test_cooldown_blocks_second_entry_same_symbol(scanner):
    bridge = FakeBridge()
    scanner.bridge = bridge

    scanner.evaluate_symbol("EURUSD")
    assert len(bridge.orders_placed) == 1

    scanner.evaluate_symbol("EURUSD")
    assert len(bridge.orders_placed) == 1, "cooldown must block a repeat entry"

    scanner.evaluate_symbol("GBPUSD")
    assert len(bridge.orders_placed) == 2, "cooldown is per symbol, not global"


def test_cooldown_expires(scanner):
    bridge = FakeBridge()
    scanner.bridge = bridge
    scanner.evaluate_symbol("EURUSD")
    assert len(bridge.orders_placed) == 1

    # Cooldown expiry permits a new setup, not a second entry on the same candle.
    scanner._last_entry_at["EURUSD"] -= (scanner.cfg.trading.symbol_cooldown_seconds + 1)
    bridge.positions = [p for p in bridge.positions if p.symbol != "EURUSD"]
    bridge.get_candles = lambda symbol, timeframe, limit=100: [
        {**row, "time": row["time"] + 900} for row in FakeBridge().get_candles(symbol, timeframe, limit)
    ]

    scanner.evaluate_symbol("EURUSD")
    assert len(bridge.orders_placed) == 2, "new candle setup must be allowed after cooldown"


def test_order_is_persisted_to_state_db(scanner):
    bridge = FakeBridge()
    scanner.bridge = bridge

    scanner.evaluate_symbol("EURUSD")

    assert len(bridge.orders_placed) == 1
    rows = scanner.db.get_orders()
    assert len(rows) == 1, "every placed order must be recorded"
    assert rows[0]["symbol"] == "EURUSD"
    assert rows[0]["status"] == "filled"
    assert rows[0]["ticket"] == 9001


def test_rejected_order_is_persisted_with_reason(scanner):
    class RejectingBridge(FakeBridge):
        def send_market_order(self, **kw):
            return FakeResult(success=False, message="market_closed")

    bridge = RejectingBridge()
    scanner.bridge = bridge

    scanner.evaluate_symbol("EURUSD")

    rows = scanner.db.get_orders()
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"


def test_failed_order_does_not_arm_cooldown(scanner):
    class RejectingBridge(FakeBridge):
        def send_market_order(self, **kw):
            return FakeResult(success=False, message="market_closed")

    bridge = RejectingBridge()
    scanner.bridge = bridge
    scanner.evaluate_symbol("EURUSD")

    assert "EURUSD" not in scanner._last_entry_at, "a rejected order must not start a cooldown"


def test_stacking_allows_entry_below_per_symbol_cap(scanner):
    """With a per-symbol cap of 3, a third EURUSD entry is still allowed."""
    scanner.cfg.trading.max_concurrent_positions = 10
    scanner.cfg.trading.max_positions_per_symbol = 3
    bridge = FakeBridge([FakePos(1, "EURUSD"), FakePos(2, "EURUSD")])
    scanner.bridge = bridge

    scanner.evaluate_symbol("EURUSD")

    assert len(bridge.orders_placed) == 1, "2 open < cap 3 must allow one more entry"


def test_stacking_blocks_entry_at_per_symbol_cap(scanner):
    """At the per-symbol cap no further entry is sent even with global room left."""
    scanner.cfg.trading.max_concurrent_positions = 10
    scanner.cfg.trading.max_positions_per_symbol = 3
    bridge = FakeBridge([FakePos(i, "EURUSD") for i in range(1, 4)])
    scanner.bridge = bridge

    scanner.evaluate_symbol("EURUSD")

    assert bridge.orders_placed == [], "3 open at cap 3 must block a 4th entry"
