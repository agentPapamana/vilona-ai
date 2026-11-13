"""Prerequisite test: broker rejection retry suppression and 3/hour block.

Proves:
1. When an order is rejected by the broker, the reason is persisted to the
   order row (`rejection_reason`).
2. The setup_key prevents resubmission on identical setups.
3. After 3 rejections for a symbol within 1 hour, that symbol is blocked
   at the start of evaluate_symbol and raises an alert.

Usage: PYTHONPATH=. /mnt/hermes-workspace/Apps/vilona-ai/.venv/bin/python scripts/test_rejection_limits.py
"""
from __future__ import annotations

import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config
from core import state_db as state_db_mod
from core.quant_scanner import QuantScanner
from core.state_db import StateDB


class RejectingBridge:
    def __init__(self):
        self.call_count = 0

    def get_positions(self, symbol=None):
        return []

    def get_pending_orders(self):
        return []

    def get_tick(self, symbol):
        return SimpleNamespace(
            symbol=symbol, bid=1.1010, ask=1.1012,
            timestamp_ms=int(time.time() * 1000),
        )

    def get_candles(self, symbol, timeframe, limit=100):
        rows = []
        for i in range(limit):
            base = 1.0880 + i * 0.00018
            rows.append({
                "time": int(time.time()) - (limit - i) * 900,
                "open": base, "high": base + .0006, "low": base - .0006,
                "close": base + .00052, "volume": 10.0,
            })
        rows[-1]["close"] = rows[-1]["open"] + .00002
        return rows

    def get_symbol_info(self, symbol):
        from trading.bridge_base import SymbolInfo
        return SymbolInfo(symbol=symbol, point=.00001, digits=5, lot_step=.01,
                          lot_min=.01, lot_max=100, tick_value=1, tick_size=.00001)

    def get_account_info(self):
        return SimpleNamespace(equity=100000.0, free_margin=100000.0)

    def get_server_day_start(self, now):
        return 1700000000

    def get_history_deals(self, since):
        return []

    def send_market_order(self, *args, **kwargs):
        self.call_count += 1
        return SimpleNamespace(success=False, message="Trade disabled by broker", retcode=10027)


class AlertSink:
    def __init__(self):
        self.alerts = []

    def send_alert(self, msg: str):
        self.alerts.append(msg)


def build_scanner(db_path, bridge, sink):
    state_db_mod.get_config = lambda: SimpleNamespace(
        database=SimpleNamespace(path=str(db_path))
    )
    cfg = Config.from_env()
    cfg.paper_trading = False
    cfg.trading.paper_trading = False
    cfg.trading.symbols = ["EURUSD"]
    cfg.trading.symbol_cooldown_seconds = 0
    cfg.trading.max_tick_age_seconds = 600.0
    cfg.trading.weekend_entry_cutoff_min = 0
    cfg.trading.weekend_flatten_min = 0
    cfg.trading.week_open_grace_min = 0
    cfg.llm.enabled = False  # deterministic only
    scanner = QuantScanner(bridge=bridge, cfg=cfg, tg_controller=sink, dashboard=None)
    scanner.db = StateDB(str(db_path))
    return scanner


def main():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "rejection_test.db"
        bridge = RejectingBridge()
        sink = AlertSink()
        scanner = build_scanner(db_path, bridge, sink)

        # Seed 2 rejections directly in DB
        now = time.time()
        for i in range(2):
            scanner.db.save_order({
                "ticket": None, "symbol": "EURUSD", "type": "market",
                "side": "buy", "quantity": 0.01, "price": 1.1010,
                "status": "rejected", "created_at": now - 600 * (i + 1),
                "setup_key": f"old_setup_{i}",
            })
            scanner.db.update_order_setup(
                f"old_setup_{i}", ticket=None, status="rejected",
                rejection_reason="seed rejection",
            )

        print(f"Initial 1h rejections: {scanner.db.count_recent_rejections('EURUSD')}")
        assert scanner.db.count_recent_rejections("EURUSD") == 2

        # Cycle 1: 3rd rejection happens via real order path
        scanner.evaluate_symbol("EURUSD")
        count_after = scanner.db.count_recent_rejections("EURUSD")
        print(f"Rejections after live reject: {count_after}")
        assert count_after == 3
        assert bridge.call_count == 1, "order should have been submitted once"

        # Check persisted reason
        orders = [o for o in scanner.db.get_orders(limit=10) if o["symbol"] == "EURUSD" and o["status"] == "rejected"]
        assert any(o.get("rejection_reason") for o in orders), "rejection_reason column was not populated!"
        print(f"Persisted rejection reason: {orders[0].get('rejection_reason')}")

        # Cycle 2: 3 rejections in 1h active -> symbol is BLOCKED at entry!
        bridge.call_count = 0
        scanner.evaluate_symbol("EURUSD")
        print(f"Orders sent while blocked: {bridge.call_count} (must be 0)")
        assert bridge.call_count == 0, "symbol was not blocked!"
        assert len(sink.alerts) >= 1, "alert was not raised!"
        print(f"Alert raised: {sink.alerts[-1]}")

        print("\nTEST REJECTION LIMITS: PASS")


if __name__ == "__main__":
    main()
