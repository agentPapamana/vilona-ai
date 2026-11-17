"""A2A (Autonomous-to-Autonomous) live test runner for scalp_5m_agresif in paper mode.

Connects to MT5 bridge, polls live ticks, allows the standalone engine to execute,
monitors open paper positions, and waits for a closed trade outcome (TP or SL).
"""
from __future__ import annotations

import logging
import sys
import time
from types import SimpleNamespace

from config import get_config
from strategy import get_standalone_scalper, get_state_manager
from trading.bridge_mt5_mcp import MT5MCPBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("a2a_test")


def run_a2a_test(symbol: str = "XAUUSDc", max_cycles: int = 120, poll_seconds: float = 3.0):
    cfg = get_config()
    cfg.trading.strategy = "scalp_5m_agresif"
    cfg.trading.symbols = [symbol]
    cfg.paper_trading = True
    cfg.trading.paper_trading = True

    bridge = MT5MCPBridge(mcp_url=cfg.exchange.mt5_mcp_url, timeout=cfg.exchange.mt5_mcp_timeout)
    state_mgr = get_state_manager()

    mock_scanner = SimpleNamespace(
        cfg=cfg,
        bridge=bridge,
        _feed=lambda msg: logger.info("[FEED] %s", msg),
        _agent_feed=lambda r, s, m: logger.info("[AGENT] %s %s: %s", r, s, m),
    )

    scalper = get_standalone_scalper(scanner=mock_scanner, config_path="scalp.yaml")

    print(f"\n=======================================================")
    print(f"  STARTING A2A SCALP 5M AGGRESIF LIVE PAPER TEST")
    print(f"  Symbol: {symbol} | Mode: PAPER | Poll: {poll_seconds}s")
    print(f"=======================================================\n")

    initial_context = state_mgr.get_short_term_context()
    print(f"Initial State: {initial_context['total_recorded_trades']} total recorded trades, {initial_context['closed_trades_count']} closed trades.\n")

    trade_opened = False
    trade_closed = False

    for cycle in range(1, max_cycles + 1):
        tick = bridge.get_tick(symbol)
        if not tick or float(getattr(tick, "bid", 0.0) or 0.0) <= 0:
            print(f"Cycle {cycle}: Waiting for live tick...")
            time.sleep(poll_seconds)
            continue

        bid = float(tick.bid)
        ask = float(tick.ask)
        spread = round(ask - bid, 4)

        # 1. Manage existing paper positions against live price (TP / SL / BE)
        scalper.manage_open_positions(bridge)

        # 2. Check if we have open paper trades
        ctx = state_mgr.get_short_term_context()
        trades = state_mgr._memory_data.get("recent_trades", [])
        open_trades = [t for t in trades if t.get("close_price") is None and t.get("exit_reason") == "OPEN"]
        closed_trades = [t for t in trades if t.get("close_price") is not None]

        if open_trades:
            trade_opened = True
            current_t = open_trades[-1]
            action = current_t["action"]
            entry = current_t["open_price"]
            sl = current_t["sl"]
            tp = current_t["tp"]
            be = current_t.get("be_triggered", False)
            dir_m = 1.0 if action == "BUY" else -1.0
            float_pts = (bid - entry) * dir_m if action == "BUY" else (entry - ask) * dir_m
            print(
                f"[CYCLE {cycle:03d}] OPEN POS #{current_t['ticket']} {action} {symbol} @ {entry:.2f} | "
                f"Cur: {bid:.2f}/{ask:.2f} | P/L: {float_pts:+.2f}pts | SL: {sl:.2f} | TP: {tp:.2f} | BE: {be}"
            )
        else:
            # If no open trade, evaluate setup
            print(f"[CYCLE {cycle:03d}] Scanning {symbol}: bid={bid:.2f} ask={ask:.2f} spread={spread:.3f}pts...")
            scalper.evaluate_symbol(symbol, bridge, tick)

        # Check if trade closed during this cycle
        new_closed_count = len(closed_trades)
        if trade_opened and not open_trades and new_closed_count > initial_context['closed_trades_count']:
            last_closed = closed_trades[-1]
            print(f"\n=======================================================")
            print(f"  TRADE COMPLETED AND CLOSED!")
            print(f"  Ticket: #{last_closed['ticket']} | {last_closed['action']} {symbol}")
            print(f"  Entry: {last_closed['open_price']} -> Exit: {last_closed['close_price']}")
            print(f"  Net P/L: ${last_closed['pnl_usd']:.2f} ({last_closed['exit_reason']})")
            print(f"  Duration: {last_closed['duration_seconds']}s | Micro-BE Triggered: {last_closed['be_triggered']}")
            print(f"=======================================================\n")
            trade_closed = True
            break

        time.sleep(poll_seconds)

    final_ctx = state_mgr.get_short_term_context()
    print("Final Short-term Memory Context:")
    import json
    print(json.dumps(final_ctx, indent=2))
    return trade_closed


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "XAUUSDc"
    run_a2a_test(sym)
