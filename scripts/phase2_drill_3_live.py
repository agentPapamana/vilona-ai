"""Phase 2 drill 3 (live-bridge edition): idempotency on LIVE MT5 data.

Pulls live tick, live candles, live account, and live day start from MT5 via the
MCP bridge. Evaluates EURUSD across 3 consecutive cycles on the same live candle,
then restarts the scanner and evaluates a 4th time. Proves:
1. First cycle records paper order intent (or exits clean on no-setup).
2. Cycles 2 and 3 produce ZERO duplicate orders for the same setup_key.
3. Post-restart cycle produces ZERO duplicate orders for the same setup_key.
4. `count_paper_setups()` on the resulting DB reports duplicate rows == 0.

Mode is strictly PAPER: zero broker orders are placed.
Usage: PYTHONPATH=. /tmp/vilvenv/bin/python scripts/phase2_drill_3_live.py
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
from scripts.judge_soak_report import count_paper_setups
from trading.bridge_mt5_mcp import MT5MCPBridge


def build_live_scanner(db_path, bridge):
    tmp_path = Path(db_path).parent
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_db_mod.get_config = lambda: SimpleNamespace(
        database=SimpleNamespace(path=str(db_path))
    )
    cfg = Config.from_env()
    # Force PAPER mode and 0 cooldown so only setup_key idempotency gates repeated calls
    cfg.paper_trading = True
    cfg.trading.paper_trading = True
    cfg.trading.symbols = ["EURUSD"]
    cfg.trading.symbol_cooldown_seconds = 0
    cfg.kill_switch_path = str(tmp_path / "NONEXISTENT")
    scanner = QuantScanner(bridge=bridge, cfg=cfg, dashboard=None)
    scanner.db = StateDB(str(db_path))
    return scanner


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    bridge = MT5MCPBridge(mcp_url="http://127.0.0.1:8765", timeout=30)
    assert bridge.connect(), "Live MT5 MCP bridge failed to connect"

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "live_idempotency.db"
        print("=== Drill 3 (Live Bridge): Idempotency across 3 cycles + restart ===")

        scanner1 = build_live_scanner(db_path, bridge)

        # Pre-flight check: verify live tick has normalized UTC timestamp
        tick = bridge.get_tick("EURUSD")
        assert tick is not None, "Live EURUSD tick unavailable"
        now_ms = int(time.time() * 1000)
        delta_s = abs(tick.timestamp_ms - now_ms) / 1000.0
        print(f"Live EURUSD tick: bid={tick.bid} ask={tick.ask} delta_s={delta_s:.2f}s")
        assert delta_s < 30.0, f"Tick not normalized to UTC! delta={delta_s:.1f}s"

        # Cycle 1
        scanner1.evaluate_symbol("EURUSD")
        orders_c1 = scanner1.db.get_orders(limit=100)
        c1_count = len(orders_c1)
        print(f"Cycle 1 orders in DB: {c1_count} (setup_key={orders_c1[0]['setup_key'] if c1_count else 'no-qualifying-setup'})")

        # Cycle 2 (immediate re-evaluation on identical live candle)
        scanner1.evaluate_symbol("EURUSD")
        orders_c2 = scanner1.db.get_orders(limit=100)
        print(f"Cycle 2 orders in DB: {len(orders_c2)} (must equal cycle 1 count {c1_count})")
        assert len(orders_c2) == c1_count, f"Duplicate order created on cycle 2! ({len(orders_c2)} vs {c1_count})"

        # Cycle 3
        scanner1.evaluate_symbol("EURUSD")
        orders_c3 = scanner1.db.get_orders(limit=100)
        print(f"Cycle 3 orders in DB: {len(orders_c3)} (must equal cycle 1 count {c1_count})")
        assert len(orders_c3) == c1_count, f"Duplicate order created on cycle 3! ({len(orders_c3)} vs {c1_count})"

        # RESTART: recreate scanner with clean in-memory state against the same DB file
        print("\n--- Simulating process restart with clean in-memory state ---")
        scanner1.db.close()
        del scanner1

        scanner2 = build_live_scanner(db_path, bridge)
        assert scanner2._last_setup_key == {}, "Restarted scanner has dirty in-memory state"

        # Cycle 4 (post-restart on identical live candle)
        scanner2.evaluate_symbol("EURUSD")
        orders_c4 = scanner2.db.get_orders(limit=100)
        print(f"Post-restart orders in DB: {len(orders_c4)} (must equal cycle 1 count {c1_count})")
        assert len(orders_c4) == c1_count, f"Duplicate order created post-restart! ({len(orders_c4)} vs {c1_count})"

        # Verify soak report calculation: duplicate rows must be 0
        paper_orders = [o for o in orders_c4 if o.get("type") == "paper"]
        distinct_setups, excluded = count_paper_setups(paper_orders)
        duplicates = max(0, len(paper_orders) - distinct_setups - excluded)
        print(f"\nSoak report metrics on live drill DB:")
        print(f"  paper rows:      {len(paper_orders)}")
        print(f"  distinct setups: {distinct_setups}")
        print(f"  duplicate rows:  {duplicates}")
        assert duplicates == 0, f"Expected 0 duplicates, got {duplicates}"

        scanner2.db.close()
        print(f"\nDRILL 3 LIVE: PASS (0 duplicate rows across 3 cycles + restart on live MT5 data)")


if __name__ == "__main__":
    main()
