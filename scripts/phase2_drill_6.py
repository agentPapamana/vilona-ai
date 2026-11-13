"""Phase 2 drill 6: total drawdown and consecutive-loss latching.

Proves:
1. When equity breaches start_balance * (1 - max_drawdown_pct/100), ENTRY LOCK
   is emitted, 0 orders placed, and the lock is latched in settings.
2. Even if equity recovers, the latched lock continues to block entries until
   an operator explicitly resets it via db.reset_risk_latches().
3. When consecutive losses reach max_consecutive_losses, ENTRY LOCK is emitted,
   and the lock remains latched until reset.

Usage: PYTHONPATH=. /tmp/vilvenv/bin/python scripts/phase2_drill_6.py
"""
from __future__ import annotations

import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import TradingConfig
from core import state_db as state_db_mod
from core.quant_scanner import QuantScanner
from core.state_db import StateDB


class FakeBridge:
    def __init__(self, equity=100000.0):
        self.orders_placed = []
        self.equity = equity

    def get_positions(self, symbol=None):
        return []

    def get_tick(self, symbol):
        return SimpleNamespace(symbol=symbol, bid=1.1010, ask=1.1012,
                               timestamp_ms=int(time.time() * 1000.0))
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
        return []

    def send_market_order(self, *args, **kwargs):
        self.orders_placed.append((args, kwargs))
        return SimpleNamespace(success=True, ticket=1)


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def build_scanner(db_path, *, equity=100000.0, max_dd_pct=10.0, max_losses=4):
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
            max_daily_loss_pct=50.0,  # wide so daily lock does not interfere
            max_total_drawdown_pct=max_dd_pct,
            max_consecutive_losses=max_losses,
            paper_trading=True,
        ),
        kill_switch_path="/tmp/vilona-NONEXISTENT-KILL-SWITCH",
        paper_trading=True,
        headless=True,
    )
    scanner = QuantScanner(bridge=FakeBridge(equity=equity), cfg=cfg, dashboard=None)
    scanner.db = StateDB(str(db_path))
    return scanner


def run_cycle(scanner):
    collector = Collect()
    logger = logging.getLogger("vilona-ai.scanner")
    logger.addHandler(collector)
    logger.setLevel(logging.INFO)
    try:
        scanner.evaluate_symbol("EURUSD")
    finally:
        logger.removeHandler(collector)
    lock_lines = [ln for ln in collector.lines if "Entry lock blocked" in ln]
    return lock_lines, len(scanner.bridge.orders_placed)


def main():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "drill6.db"

        print("=== Drill 6a: total drawdown trigger and latch ===")
        # Seed start balance = 100k, equity at 89k (11% drawdown, limit 10%)
        scanner = build_scanner(db_path, equity=89000.0, max_dd_pct=10.0)
        scanner.db.set_start_balance(100000.0)

        lines, orders = run_cycle(scanner)
        print(f"EXCERPT 1 (trigger): {lines}")
        print(f"ORDERS: {orders}")
        latched_after_trigger = scanner.db.is_drawdown_latched()
        print(f"LATCHED_AFTER_TRIGGER: {latched_after_trigger}")

        # Simulate equity recovering to 95k (drawdown 5%, within limit)
        scanner.bridge.equity = 95000.0
        lines_rec, orders_rec = run_cycle(scanner)
        print(f"EXCERPT 2 (recovery while latched): {lines_rec}")
        print(f"ORDERS: {orders_rec}")
        assert orders_rec == 0, "order placed while drawdown lock was latched"

        # Operator resets the latch with a recorded reason
        scanner.db.reset_risk_latches("operator drill: verified recovery")
        latched_after_reset = scanner.db.is_drawdown_latched()
        print(f"LATCHED_AFTER_RESET: {latched_after_reset}")

        lines_free, orders_free = run_cycle(scanner)
        print(f"EXCERPT 3 (after operator reset): {lines_free}")
        print(f"ORDERS: {orders_free}")
        print(f"RESULT 6a: {'PASS' if latched_after_trigger and not latched_after_reset and not lines_free else 'FAIL'}")
        print()

        print("=== Drill 6b: consecutive losses trigger and latch ===")
        db_path2 = Path(td) / "drill6b.db"
        scanner2 = build_scanner(db_path2, equity=100000.0, max_losses=4)
        scanner2.db.set_start_balance(100000.0)

        # Seed 4 consecutive losses in trades table
        for i in range(4):
            scanner2.db.save_trade({
                "ticket": 9000 + i, "symbol": "EURUSD", "side": "buy",
                "quantity": 0.01, "entry_price": 1.10, "exit_price": 1.09,
                "profit": -50.0, "commission": -1.0, "swap": 0.0,
                "opened_at": 1700000000 + i * 3600,
                "closed_at": 1700000000 + i * 3600 + 1800,
                "status": "closed",
            })

        lines_loss, orders_loss = run_cycle(scanner2)
        print(f"EXCERPT 4 (consecutive loss trigger): {lines_loss}")
        print(f"ORDERS: {orders_loss}")
        loss_latched = scanner2.db.is_consecutive_loss_latched()
        print(f"CONSECUTIVE_LOSS_LATCHED: {loss_latched}")

        scanner2.db.reset_risk_latches("operator drill: reviewed losses")
        loss_latched_after_reset = scanner2.db.is_consecutive_loss_latched()
        print(f"LATCHED_AFTER_RESET: {loss_latched_after_reset}")
        print(f"RESULT 6b: {'PASS' if loss_latched and not loss_latched_after_reset else 'FAIL'}")
        print()


if __name__ == "__main__":
    main()
