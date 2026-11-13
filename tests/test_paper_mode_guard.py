"""Guard test: paper_trading must never reach the execution bridge.

Previously the scanner called place_protected_order unconditionally, so the
"Mode: PAPER" label was cosmetic and a qualifying signal would have placed a
real order. These tests fail if that guard is ever removed.
"""

from __future__ import annotations
import time

from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest

from config import Config
from core.quant_scanner import QuantScanner


@pytest.fixture(autouse=True)
def isolated_state_db(tmp_path, monkeypatch):
    """Keep guard tests out of the developer's live paper database."""
    from types import SimpleNamespace
    from core import state_db as state_db_mod
    monkeypatch.setattr(state_db_mod, "get_config", lambda: SimpleNamespace(
        database=SimpleNamespace(path=str(tmp_path / "guard_test.db"))
    ))


@dataclass
class FakeTick:
    symbol: str
    bid: float
    ask: float
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))


class RecordingBridge:
    """Bridge stub that records every mutating call and fabricates a BUY setup."""

    def __init__(self) -> None:
        self.order_calls: List[Dict[str, Any]] = []
        self.modify_calls: List[Dict[str, Any]] = []

    # --- read-only surface -------------------------------------------------
    def get_positions(self):
        return []

    def get_pending_orders(self):
        return []

    def get_tick(self, symbol):
        # Price must sit within 1.2 ATR of EMA21 for the pullback filter to
        # qualify; the candle fixture puts EMA21 near 1.10035.
        return FakeTick(symbol=symbol, bid=1.10045, ask=1.10055)

    def get_candles(self, symbol, timeframe, limit=100):
        # Uptrend closing candles so EMA21 > EMA50 and price sits near EMA21.
        out = []
        for i in range(limit):
            px = 1.0900 + i * 0.00015
            out.append({
                "time": 1700000000 + i * 900,
                "open": px - 0.00002,
                "high": px + 0.00005,
                "low": px - 0.00005,
                "close": px,
                "tick_volume": 100 + (i % 7),
                "volume": 100 + (i % 7),
            })
        return out

    def get_account_info(self):
        return type("Acct", (), {
            "balance": 100000.0, "equity": 100000.0, "margin": 0.0,
            "free_margin": 100000.0, "login": 1, "server": "MetaQuotes-Demo",
        })()

    def get_symbol_info(self, symbol):
        return type("Info", (), {
            "digits": 5, "point": 0.00001, "stops_level": 0,
            "tick_value": 1.0, "tick_size": 0.00001,
            "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        })()

    def get_server_day_start(self, now_epoch):
        return 1700000000

    def get_history_deals(self, since_epoch):
        return []

    # --- mutating surface: must stay untouched in paper mode ----------------
    def send_market_order(self, *a, **k):
        self.order_calls.append({"a": a, "k": k})
        return type("R", (), {"success": True, "message": "ok", "ticket": 1})()

    def place_order(self, *a, **k):
        self.order_calls.append({"a": a, "k": k})
        return type("R", (), {"success": True, "message": "ok", "ticket": 1})()

    def modify_position_sltp(self, *a, **k):
        self.modify_calls.append({"a": a, "k": k})
        return True

    def close_position(self, *a, **k):
        self.modify_calls.append({"a": a, "k": k})
        return True


def _cfg(paper: bool) -> Config:
    cfg = Config()
    cfg.exchange.name = "mt5"
    cfg.trading.symbols = ["EURUSD"]
    cfg.trading.min_confidence = 0.0
    cfg.trading.symbol_cooldown_seconds = 0
    cfg.paper_trading = paper
    cfg.llm.enabled = False
    return cfg


def test_paper_mode_places_no_order():
    bridge = RecordingBridge()
    scanner = QuantScanner(bridge, _cfg(paper=True))
    scanner.evaluate_symbol("EURUSD")
    assert bridge.order_calls == [], "paper mode must never place an order"


def test_paper_mode_does_not_modify_positions():
    bridge = RecordingBridge()
    scanner = QuantScanner(bridge, _cfg(paper=True))
    scanner.evaluate_symbol("EURUSD")
    assert bridge.modify_calls == []


def test_paper_mode_registers_cooldown_so_it_does_not_rescan_every_cycle():
    bridge = RecordingBridge()
    scanner = QuantScanner(bridge, _cfg(paper=True))
    scanner.cfg.trading.symbol_cooldown_seconds = 300
    scanner.evaluate_symbol("EURUSD")
    assert "EURUSD" in scanner._last_entry_at
    assert bridge.order_calls == []


def test_repeated_scan_of_unchanged_setup_records_one_paper_row(monkeypatch, tmp_path):
    """Duplicate polling of the same setup must not inflate paper evidence.

    A cooldown alone was not enough: the same untouched setup kept producing a
    new paper row every cycle, so the soak count measured polling frequency.
    """
    from types import SimpleNamespace
    from core import state_db as state_db_mod

    db_path = str(tmp_path / "paper_dup.db")
    monkeypatch.setattr(state_db_mod, "get_config", lambda: SimpleNamespace(
        database=SimpleNamespace(path=db_path)
    ))

    bridge = RecordingBridge()
    scanner = QuantScanner(bridge, _cfg(paper=True))
    scanner.cfg.trading.symbol_cooldown_seconds = 300

    scanner.evaluate_symbol("EURUSD")
    bridge.get_tick = lambda symbol: FakeTick(symbol=symbol, bid=1.10060, ask=1.10070)
    scanner = QuantScanner(bridge, _cfg(paper=True))
    scanner.cfg.trading.symbol_cooldown_seconds = 300
    scanner.evaluate_symbol("EURUSD")

    rows = [r for r in scanner.db.get_orders() if r["type"] == "paper"]
    assert len(rows) == 1, "same candle setup must not duplicate after restart or price movement"
    assert bridge.order_calls == []


def test_live_mode_would_call_the_bridge(monkeypatch):
    """Control test: proves the guard is the only thing blocking execution.

    If paper mode were removed the bridge call must happen, otherwise the
    guard tests above could pass for the wrong reason (setup never qualifying).
    """
    bridge = RecordingBridge()
    scanner = QuantScanner(bridge, _cfg(paper=False))

    import core.order_safety as safety

    called = {"n": 0}

    def fake_place(*a, **k):
        called["n"] += 1
        bridge.order_calls.append({"a": a, "k": k})
        return type("P", (), {"success": False, "message": "stub", "sl_verified": False})()

    monkeypatch.setattr(safety, "place_protected_order", fake_place)
    scanner.evaluate_symbol("EURUSD")
    assert called["n"] >= 1, "setup fixture must qualify, else paper tests are vacuous"


def test_scanner_start_actually_spawns_alive_thread():
    """Regression: scanner.start() must actually launch its background thread."""
    scanner = QuantScanner(RecordingBridge(), _cfg(paper=True))
    scanner.poll_interval = 3600.0  # one cycle, then park
    try:
        scanner.start()
        assert scanner._thread is not None
        assert scanner._thread.is_alive()
    finally:
        scanner.stop()
    assert not scanner._thread.is_alive()
