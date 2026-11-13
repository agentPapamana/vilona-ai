from __future__ import annotations
import time

from types import SimpleNamespace

import pytest

from config import TradingConfig
from core.quant_scanner import QuantScanner
from core.risk_lock import write_kill_switch
from core import state_db as state_db_mod


class FakeBridge:
    def __init__(self):
        self.orders_placed = []

    def get_positions(self, symbol=None): return []
    def get_tick(self, symbol): return SimpleNamespace(symbol=symbol, bid=1.1010, ask=1.1012, timestamp_ms=int(time.time()*1000))
    def get_candles(self, symbol, timeframe, limit=100):
        rows = []
        for i in range(limit):
            base = 1.0880 + i * 0.00018
            rows.append({"time": 1700000000 + i * 900, "open": base, "high": base + .00060,
                         "low": base - .00060, "close": base + .00052, "volume": 10.0})
        rows[-1]["close"] = rows[-1]["open"] + .00002
        return rows
    def get_symbol_info(self, symbol):
        from trading.bridge_base import SymbolInfo
        return SymbolInfo(symbol=symbol, point=.00001, digits=5, lot_step=.01, lot_min=.01,
                          lot_max=100, tick_value=1, tick_size=.00001)
    def get_account_info(self): return SimpleNamespace(equity=100000, free_margin=100000)
    def get_server_day_start(self, now): return 1700000000
    def get_history_deals(self, since): return []
    def send_market_order(self, *args, **kwargs): self.orders_placed.append((args, kwargs)); return SimpleNamespace(success=True, ticket=1)


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db_mod, "get_config", lambda: SimpleNamespace(
        database=SimpleNamespace(path=str(tmp_path / "risk_guard.db"))
    ))
    cfg = SimpleNamespace(trading=TradingConfig(
        symbols=["EURUSD"], max_concurrent_positions=3,
        max_positions_per_symbol=1, min_confidence=0.0,
        symbol_cooldown_seconds=300, max_daily_loss_pct=2.0,
    ), kill_switch_path=str(tmp_path / "STOP_ENTRIES"), paper_trading=False)
    return QuantScanner(bridge=FakeBridge(), cfg=cfg, dashboard=None)


@pytest.mark.parametrize("mode", ["kill", "unavailable_history", "threshold"])
def test_entry_guard_prevents_broker_order(scanner, mode):
    bridge = scanner.bridge
    if mode == "kill":
        write_kill_switch(scanner.cfg.kill_switch_path, True)
    elif mode == "unavailable_history":
        bridge.get_server_day_start = lambda now: 100
        bridge.get_history_deals = lambda since: None
        bridge.get_account_info = lambda: None
    else:
        bridge.get_server_day_start = lambda now: 100
        bridge.get_history_deals = lambda since: [{"profit": -2000}]
    scanner.evaluate_symbol("EURUSD")
    assert bridge.orders_placed == []
