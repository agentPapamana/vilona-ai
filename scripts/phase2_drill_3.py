"""Phase 2 drill 3: idempotency across repeated polling cycles and restart.

Proves:
1. When evaluate_symbol runs 3 consecutive cycles on the same unchanged candle,
   only ONE order intent is saved to the DB (duplicate insert returns False).
2. After a scanner restart (new QuantScanner instance, clean in-memory state),
   evaluating the same candle again STILL produces zero duplicate rows because
   the durable setup_key constraint in StateDB catches it.
3. judge_soak_report count_paper_setups verifies duplicate rows == 0.

Usage: PYTHONPATH=. /tmp/vilvenv/bin/python scripts/phase2_drill_3.py
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
from scripts.judge_soak_report import count_paper_setups


class FixedCandleBridge:
    """Bridge with pinned fresh tick and pinned candles to guarantee identical setup."""

    def __init__(self):
        self.orders_placed = []
        # Candle timestamp is fixed so setup_key = symbol|action|time is identical every cycle.
        self.candle_time = 1700000000

    def get_positions(self, symbol=None):
        return []

    def get_pending_orders(self):
        return []

    def get_tick(self, symbol):
        # Tick is always fresh so stale-tick gate does not trip.
        return SimpleNamespace(
            symbol=symbol,
            bid=1.1010,
            ask=1.1012,
            timestamp_ms=int(time.time() * 1000.0),
        )

    def get_candles(self, symbol, timeframe, limit=100):
        rows = []
        for i in range(limit):
            base = 1.0880 + i * 0.00018
            rows.append({
                "time": self.candle_time + i * 900,
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

    def send_market_order(self, *args, **kwargs):
        self.orders_placed.append((args, kwargs))
        return SimpleNamespace(success=True, ticket=1)


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
            symbol_cooldown_seconds=0,  # 0 cooldown so only setup_key idempotency gates it
            max_daily_loss_pct=50.0,
            max_total_drawdown_pct=50.0,
            max_consecutive_losses=10,
            max_tick_age_seconds=60.0,
            weekend_entry_cutoff_min=0,  # disable weekend filter for drill
            weekend_flatten_min=0,
            week_open_grace_min=0,
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "drill3.db"

        print("=== Drill 3: Idempotency across 3 cycles + restart ===")
        bridge = FixedCandleBridge()
        scanner1 = build_scanner(db_path, bridge)

        # Cycle 1: initial setup recorded
        scanner1.evaluate_symbol("EURUSD")
        orders_c1 = scanner1.db.get_orders(limit=100)
        print(f"Cycle 1 orders in DB: {len(orders_c1)} (setup_key={orders_c1[0]['setup_key'] if orders_c1 else None})")
        assert len(orders_c1) == 1, f"expected 1 order, got {len(orders_c1)}"

        # Cycle 2: same candle polled again
        scanner1.evaluate_symbol("EURUSD")
        orders_c2 = scanner1.db.get_orders(limit=100)
        print(f"Cycle 2 orders in DB: {len(orders_c2)} (must remain 1)")
        assert len(orders_c2) == 1, f"duplicate order created on cycle 2: {len(orders_c2)}"

        # Cycle 3: same candle polled again
        scanner1.evaluate_symbol("EURUSD")
        orders_c3 = scanner1.db.get_orders(limit=100)
        print(f"Cycle 3 orders in DB: {len(orders_c3)} (must remain 1)")
        assert len(orders_c3) == 1, f"duplicate order created on cycle 3: {len(orders_c3)}"

        # RESTART: Simulate process restart (clean memory, same DB file)
        print("\n--- Simulating process restart ---")
        scanner1.db.close()
        del scanner1

        bridge2 = FixedCandleBridge()
        scanner2 = build_scanner(db_path, bridge2)
        assert scanner2._last_setup_key == {}, "fresh scanner has dirty in-memory state"

        # Cycle 4 (post-restart): same candle polled again
        scanner2.evaluate_symbol("EURUSD")
        orders_c4 = scanner2.db.get_orders(limit=100)
        print(f"Post-restart orders in DB: {len(orders_c4)} (must remain 1)")
        assert len(orders_c4) == 1, f"duplicate order created post-restart: {len(orders_c4)}"

        # Verify soak report calculation: duplicate rows must be 0
        paper_orders = [o for o in orders_c4 if o.get("type") == "paper"]
        distinct_setups, excluded = count_paper_setups(paper_orders)
        duplicates = max(0, len(paper_orders) - distinct_setups - excluded)
        print(f"\nSoak report metrics on drill DB:")
        print(f"  paper rows:      {len(paper_orders)}")
        print(f"  distinct setups: {distinct_setups}")
        print(f"  duplicate rows:  {duplicates}")
        assert duplicates == 0, f"expected 0 duplicates, got {duplicates}"
        assert distinct_setups == 1, f"expected 1 distinct setup, got {distinct_setups}"

        scanner2.db.close()
        print("\nDRILL 3: PASS (0 duplicate rows across 3 cycles + restart)")


if __name__ == "__main__":
    main()
