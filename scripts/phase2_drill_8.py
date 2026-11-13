"""Phase 2 drill 8: weekend entry cutoff and weekend flatten intent in PAPER.

Proves:
1. When broker tick time is inside Friday entry cutoff (last 120m before 22:00 UTC),
   evaluate_symbol emits ENTRY LOCK and 0 orders placed.
2. When broker tick time is in Friday flatten window (last 60m), manage_open_positions
   emits WEEKEND FLATTEN intent for Vilona-owned positions (magic in range).
3. Manual/foreign positions (magic=0) are untouched by flatten.
4. PAPER mode records intent and never calls bridge.close_position.

Usage: PYTHONPATH=. /tmp/vilvenv/bin/python scripts/phase2_drill_8.py
"""
from __future__ import annotations

import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import TradingConfig
from core import state_db as state_db_mod
from core.order_safety import MAGIC_BASE
from core.quant_scanner import QuantScanner
from core.state_db import StateDB


def _epoch_at(year=2026, month=11, day=13, hour=12, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()


class FakePos:
    def __init__(self, ticket: int, symbol: str, magic: int = 0):
        self.ticket = ticket
        self.symbol = symbol
        self.magic = magic
        self.type = 0
        self.price_open = 1.10
        self.price_current = 1.10
        self.sl = 1.09
        self.tp = 1.12
        self.volume = 0.01


class FakeBridge:
    def __init__(self, tick_time_s: float, positions=None):
        self.tick_time_s = tick_time_s
        self.open_positions = positions or []
        self.close_calls = []
        self.orders_placed = []

    def get_positions(self, symbol=None):
        return list(self.open_positions)

    def get_tick(self, symbol):
        return SimpleNamespace(
            symbol=symbol,
            bid=1.1010,
            ask=1.1012,
            timestamp_ms=int(self.tick_time_s * 1000.0),
        )

    def get_candles(self, symbol, timeframe, limit=100):
        rows = []
        for i in range(limit):
            base = 1.0880 + i * 0.00018
            rows.append({
                "time": int(self.tick_time_s) - (limit - i) * 900,
                "open": base,
                "high": base + .00060,
                "low": base - .00060,
                "close": base + .00052,
                "volume": 10.0,
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

    def close_position(self, *args, **kwargs):
        self.close_calls.append((args, kwargs))
        return SimpleNamespace(success=True, message="closed")

    def send_market_order(self, *args, **kwargs):
        self.orders_placed.append((args, kwargs))
        return SimpleNamespace(success=True, ticket=1)


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def build_scanner(db_path, bridge):
    state_db_mod.get_config = lambda: SimpleNamespace(
        database=SimpleNamespace(path=str(db_path))
    )
    cfg = SimpleNamespace(
        trading=TradingConfig(
            symbols=["EURUSD"],
            max_concurrent_positions=1,
            max_positions_per_symbol=1,
            min_confidence=0.0,
            symbol_cooldown_seconds=300,
            max_daily_loss_pct=50.0,
            max_total_drawdown_pct=50.0,
            max_consecutive_losses=10,
            max_tick_age_seconds=86400 * 365,  # wide so stale check doesn't trip on simulated dates
            weekend_entry_cutoff_min=120,
            weekend_flatten_min=60,
            week_open_grace_min=30,
            paper_trading=True,
        ),
        kill_switch_path="/tmp/vilona-NONEXISTENT",
        paper_trading=True,
        headless=True,
    )
    scanner = QuantScanner(bridge=bridge, cfg=cfg, dashboard=None)
    scanner.db = StateDB(str(db_path))
    return scanner


def main():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "drill8.db"

        print("=== Drill 8a: Friday pre-close entry cutoff (20:15 UTC, close at 22:00) ===")
        t_friday_cutoff = _epoch_at(2026, 11, 6, 20, 15)  # 105m before close (<120m cutoff)
        bridge_cutoff = FakeBridge(tick_time_s=t_friday_cutoff)
        scanner_cutoff = build_scanner(db_path, bridge_cutoff)

        collector = Collect()
        logger = logging.getLogger("vilona-ai.scanner")
        logger.addHandler(collector)
        logger.setLevel(logging.INFO)
        try:
            scanner_cutoff.evaluate_symbol("EURUSD")
        finally:
            logger.removeHandler(collector)

        lock_lines = [ln for ln in collector.lines if "Weekend guard blocked" in ln]
        print(f"EXCERPT 1: {lock_lines}")
        print(f"ORDERS_PLACED: {len(bridge_cutoff.orders_placed)}")
        assert len(bridge_cutoff.orders_placed) == 0
        assert lock_lines and "cutoff" in lock_lines[0]
        print("RESULT 8a: PASS\n")

        print("=== Drill 8b: Friday pre-close flatten intent in PAPER (21:30 UTC) ===")
        t_friday_flatten = _epoch_at(2026, 11, 6, 21, 30)  # 30m before close (<60m flatten)
        vilona_pos = FakePos(ticket=7001, symbol="EURUSD", magic=MAGIC_BASE + 1)
        manual_pos = FakePos(ticket=9999, symbol="EURUSD", magic=0)
        bridge_flatten = FakeBridge(tick_time_s=t_friday_flatten, positions=[vilona_pos, manual_pos])
        scanner_flatten = build_scanner(db_path, bridge_flatten)

        collector2 = Collect()
        logger.addHandler(collector2)
        try:
            scanner_flatten.manage_open_positions()
        finally:
            logger.removeHandler(collector2)

        flatten_lines = [ln for ln in collector2.lines if "Weekend flatten intent" in ln]
        print(f"EXCERPT 2: {flatten_lines}")
        print(f"BRIDGE_CLOSE_CALLS: {len(bridge_flatten.close_calls)} (must be 0 in PAPER)")
        # Vilona pos got intent emitted; manual pos did NOT; bridge was NOT called
        assert len(bridge_flatten.close_calls) == 0, "PAPER mode mutated bridge on flatten"
        assert len(flatten_lines) == 1, f"expected 1 flatten intent, got {flatten_lines}"
        assert "7001" in flatten_lines[0] and "9999" not in flatten_lines[0]
        print("RESULT 8b: PASS\n")

        print("=== Drill 8c: Sunday open grace entry cutoff (21:15 UTC, open at 21:00) ===")
        t_sunday_grace = _epoch_at(2026, 11, 8, 21, 15)  # 15m after open (<30m grace)
        bridge_grace = FakeBridge(tick_time_s=t_sunday_grace)
        scanner_grace = build_scanner(db_path, bridge_grace)

        collector3 = Collect()
        logger.addHandler(collector3)
        try:
            scanner_grace.evaluate_symbol("EURUSD")
        finally:
            logger.removeHandler(collector3)

        grace_lines = [ln for ln in collector3.lines if "Weekend guard blocked" in ln]
        print(f"EXCERPT 3: {grace_lines}")
        assert len(bridge_grace.orders_placed) == 0
        assert grace_lines and "grace" in grace_lines[0]
        print("RESULT 8c: PASS\n")


if __name__ == "__main__":
    main()
