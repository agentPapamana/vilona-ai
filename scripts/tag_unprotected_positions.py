#!/usr/bin/env python3
"""Ensure open positions have protective stop losses.

Dry-run by default. Use --once for one pass, or --loop SECONDS to monitor
continuously beside the scanner. Mutation requires --apply explicitly.

Usage:
    python scripts/tag_unprotected_positions.py --once
    python scripts/tag_unprotected_positions.py --apply --once
    python scripts/tag_unprotected_positions.py --apply --loop 60
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.quant_scanner import compute_atr
from trading.bridge_mt5_mcp import MT5MCPBridge

SL_ATR_MULT = 1.5
DEFAULT_STOP_PCT = 0.005  # fallback when ATR is unavailable


def position_side(pos) -> str:
    raw = getattr(pos, "type", "")
    if isinstance(raw, (int, float)):
        return "buy" if int(raw) == 0 else "sell"
    s = str(raw or "").lower()
    if s in ("0", "buy", "long", "position_type_buy"):
        return "buy"
    if s in ("1", "sell", "short", "position_type_sell"):
        return "sell"
    return s


def is_buy(pos) -> bool:
    side = position_side(pos)
    if side in ("0", "buy", "position_type_buy", "long"):
        return True
    if side in ("1", "sell", "position_type_sell", "short"):
        return False
    raise ValueError(f"unsupported position side: {side!r}")


def has_stop(pos) -> bool:
    return float(getattr(pos, "sl", 0.0) or 0.0) > 0.0


def target_sl(pos, atr: float) -> float:
    entry = float(pos.price_open)
    dist = atr * SL_ATR_MULT if atr > 0 else entry * DEFAULT_STOP_PCT
    sl = entry - dist if is_buy(pos) else entry + dist
    return round(sl, 5)


def run_pass(bridge: MT5MCPBridge, apply: bool) -> int:
    positions = bridge.get_positions() or []
    unprotected = [p for p in positions if not has_stop(p)]

    print(f"open positions: {len(positions)}  unprotected: {len(unprotected)}")
    if not unprotected:
        return 0

    atr_cache: dict[str, float] = {}
    rc = 0
    for pos in unprotected:
        symbol = pos.symbol
        try:
            if symbol not in atr_cache:
                candles = bridge.get_candles(symbol, "15m", limit=60)
                atr_cache[symbol] = compute_atr(candles, 14)
            atr = atr_cache[symbol]
            sl = target_sl(pos, atr)
        except Exception as exc:
            # Never abort the sweep: a bad side/price on one position must not
            # leave every later position unprotected.
            print(f"  #{pos.ticket} {symbol} SKIPPED: {exc}")
            rc = 1
            continue

        print(
            f"  #{pos.ticket} {symbol} {position_side(pos)} vol={pos.volume} "
            f"entry={pos.price_open} cur={pos.price_current} atr={atr:.5f} -> SL={sl}"
        )

        if not apply:
            continue

        try:
            result = bridge.modify_position_sltp(
                ticket=int(pos.ticket), symbol=symbol, sl=sl, tp=None
            )
        except Exception as exc:
            print(f"    set SL {sl}: FAILED: {exc}")
            rc = 1
            continue
        status = "OK" if result.success else f"FAILED: {result.message}"
        print(f"    set SL {sl}: {status}")
        if not result.success:
            rc = 1

    if apply:
        # Re-read to prove the change actually landed.
        after = bridge.get_positions() or []
        still = [p for p in after if not has_stop(p)]
        print(f"verification: {len(still)} position(s) still without SL")
        for p in still:
            print(f"  #{p.ticket} {p.symbol} vol={p.volume}")
        if still:
            rc = 1
    else:
        print("\ndry-run only. re-run with --apply to attach these stop losses.")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually modify positions")
    ap.add_argument("--once", action="store_true", help="single pass (implied by --apply without --loop)")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="repeat forever every SECONDS")
    args = ap.parse_args()

    bridge = MT5MCPBridge("http://127.0.0.1:8765")
    if not bridge.connect():
        print("connect to MT5 MCP failed", file=sys.stderr)
        return 2

    if args.loop <= 0:
        return run_pass(bridge, args.apply)

    interval = max(10, args.loop)
    print(f"looping every {interval}s (apply={args.apply})")
    rc = 0
    while True:
        try:
            rc |= run_pass(bridge, args.apply)
        except KeyboardInterrupt:
            return rc
        except Exception as exc:  # keep the guard alive; a crash would leave positions bare
            print(f"pass failed: {exc}", file=sys.stderr)
            rc = 1
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
