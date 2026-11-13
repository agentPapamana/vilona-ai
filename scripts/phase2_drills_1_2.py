"""Phase 2 drills 1-2 in PAPER: kill switch and daily-loss entry lock.

Runs the production QuantScanner.evaluate_symbol() against a fake bridge and a
temp DB, so the observed log lines come from real code paths, not a re-implemented
assertion. Prints a log excerpt per drill for the evidence log.

Usage: PYTHONPATH=. python scripts/phase2_drills_1_2.py
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import TradingConfig
from core import state_db as state_db_mod
from core.quant_scanner import QuantScanner
from core.risk_lock import write_kill_switch


class FakeBridge:
    """Paper bridge: no broker, records any order attempt."""

    def __init__(self):
        self.orders_placed = []
        self.daily_profit = 0.0
        self.equity = 100000.0

    def get_positions(self, symbol=None):
        return []

    def get_tick(self, symbol):
        return SimpleNamespace(symbol=symbol, bid=1.1010, ask=1.1012)

    def get_candles(self, symbol, timeframe, limit=100):
        rows = []
        for i in range(limit):
            base = 1.0880 + i * 0.00018
            rows.append({
                "time": 1700000000 + i * 900,
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
        return SimpleNamespace(equity=self.equity, free_margin=self.equity)

    def get_server_day_start(self, now):
        return 1700000000

    def get_history_deals(self, since):
        return [{"profit": self.daily_profit}]

    def send_market_order(self, *args, **kwargs):
        self.orders_placed.append((args, kwargs))
        return SimpleNamespace(success=True, ticket=1)


def build_scanner(tmp_path, max_daily_loss_pct):
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_db_mod.get_config = lambda: SimpleNamespace(
        database=SimpleNamespace(path=str(tmp_path / "phase2.db"))
    )
    cfg = SimpleNamespace(
        trading=TradingConfig(
            symbols=["EURUSD"],
            max_concurrent_positions=1,
            max_positions_per_symbol=1,
            min_confidence=0.0,
            symbol_cooldown_seconds=300,
            max_daily_loss_pct=max_daily_loss_pct,
            paper_trading=True,
        ),
        kill_switch_path=str(tmp_path / "STOP_ENTRIES"),
        paper_trading=True,
        headless=True,
    )
    return QuantScanner(bridge=FakeBridge(), cfg=cfg, dashboard=None)


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def run_drill(name, tmp_path, *, max_daily_loss_pct=2.0, kill=False, daily_profit=0.0):
    scanner = build_scanner(tmp_path, max_daily_loss_pct)
    scanner.bridge.daily_profit = daily_profit
    if kill:
        write_kill_switch(scanner.cfg.kill_switch_path, True)

    logger = logging.getLogger("vilona-ai.scanner")
    collector = Collect()
    logger.addHandler(collector)
    logger.setLevel(logging.INFO)
    try:
        scanner.evaluate_symbol("EURUSD")
    finally:
        logger.removeHandler(collector)

    lock_lines = [ln for ln in collector.lines if "Entry lock blocked" in ln]
    orders = len(scanner.bridge.orders_placed)
    if kill:
        write_kill_switch(scanner.cfg.kill_switch_path, False)

    print(f"--- {name} ---")
    for line in lock_lines:
        print(f"LOG: {line}")
    if not lock_lines:
        print("LOG: (no entry lock emitted)")
    print(f"ORDERS_SENT: {orders}")
    print()
    return lock_lines, orders


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        lock1, orders1 = run_drill("Drill 1: kill switch active", root / "d1", kill=True)
        print("EXPECT: ENTRY LOCK due to operator kill switch; 0 orders")
        print(f"RESULT: {'PASS' if lock1 and orders1 == 0 and 'kill switch' in lock1[0] else 'FAIL'}")
        print()

        lock2, orders2 = run_drill(
            "Drill 2: daily-loss threshold exceeded",
            root / "d2",
            max_daily_loss_pct=0.01,
            daily_profit=-100.0,
        )
        print("EXPECT: ENTRY LOCK due to daily loss threshold; 0 orders")
        print(f"RESULT: {'PASS' if lock2 and orders2 == 0 and 'daily loss' in lock2[0] else 'FAIL'}")
        print()

        lock1b, orders1b = run_drill("Drill 1b: kill switch removed", root / "d1b", kill=False)
        print("EXPECT: no kill-switch lock; scanning proceeds")
        print(f"RESULT: {'PASS' if not any('kill switch' in ln for ln in lock1b) else 'FAIL'}")
        print()


if __name__ == "__main__":
    main()
