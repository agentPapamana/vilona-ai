"""Paginating historical candle downloader for the B9 backtest gate.

The live tail read (`get_candles`) is capped at 500 bars by the EA (~7 days on
M15). Six months of 15m candles is ~17,280 bars, so this script walks the
timeline backwards using the dated `get_candles_range` tool in 1000-bar pages,
deduplicates by bar timestamp, and saves to `data/candles/{symbol}_15m.json`.

Usage:
    python scripts/download_candles.py --months 6
    python scripts/download_candles.py --symbols XAUUSD --months 6
    python scripts/download_candles.py --timeframe H1 --months 12
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading.bridge_mt5_mcp import MT5MCPBridge

DEFAULT_SYMBOLS = ("XAUUSD", "EURUSD", "USDJPY")
DEFAULT_DEST = Path(__file__).resolve().parents[1] / "data" / "candles"
PAGE_BARS = 1000


def download_symbol(
    bridge: MT5MCPBridge,
    symbol: str,
    timeframe: str,
    from_epoch: int,
    to_epoch: int,
    page_size: int = PAGE_BARS,
    delay_s: float = 0.1,
) -> list[dict[str, Any]]:
    """Walk backward from `to_epoch` to `from_epoch`, oldest-first within each page."""
    bars_by_time: dict[int, dict[str, Any]] = {}
    current_to = to_epoch
    page_count = 0

    print(f"[{symbol}] Downloading {timeframe} from {from_epoch} to {to_epoch}...")
    while current_to > from_epoch:
        page = bridge.get_candles_range(
            symbol=symbol,
            timeframe=timeframe,
            from_epoch=from_epoch,
            to_epoch=current_to,
            limit=page_size,
        )
        if not page:
            print(f"[{symbol}] Page {page_count + 1}: no more bars returned before {current_to}")
            break

        new_bars = 0
        oldest_in_page = current_to
        for bar in page:
            t = int(bar.get("time", 0))
            if t and t not in bars_by_time:
                bars_by_time[t] = bar
                new_bars += 1
            if t and t < oldest_in_page:
                oldest_in_page = t

        page_count += 1
        span_d = (max(bars_by_time) - min(bars_by_time)) / 86400.0 if bars_by_time else 0.0
        print(f"[{symbol}] Page {page_count}: +{new_bars} bars (total {len(bars_by_time)}, {span_d:.1f}d) oldest={oldest_in_page}")

        # If the page did not advance, we hit the terminal's history floor.
        if oldest_in_page >= current_to or new_bars == 0:
            print(f"[{symbol}] Reached history floor at {oldest_in_page}")
            break

        current_to = oldest_in_page - 1
        if delay_s > 0:
            time.sleep(delay_s)

    ordered = [bars_by_time[t] for t in sorted(bars_by_time)]
    return ordered


def main() -> int:
    ap = argparse.ArgumentParser(description="Download historical candles from MT5 via MCP.")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--dest-dir", type=Path, default=DEFAULT_DEST)
    ap.add_argument("--page-size", type=int, default=PAGE_BARS)
    ap.add_argument("--delay", type=float, default=0.05)
    args = ap.parse_args()

    args.dest_dir.mkdir(parents=True, exist_ok=True)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    bridge = MT5MCPBridge("http://127.0.0.1:8765", timeout=60)
    if not bridge.connect():
        print("ERROR: could not connect to MT5 MCP bridge on http://127.0.0.1:8765", file=sys.stderr)
        return 1

    now = int(time.time())
    from_epoch = now - (args.months * 30 * 86400)
    print(f"Destination: {args.dest_dir}")
    print(f"Window: {args.months} months ({from_epoch} -> {now})")

    failed = 0
    for sym in symbols:
        try:
            bars = download_symbol(
                bridge=bridge,
                symbol=sym,
                timeframe=args.timeframe,
                from_epoch=from_epoch,
                to_epoch=now,
                page_size=args.page_size,
                delay_s=args.delay,
            )
            if not bars:
                print(f"[{sym}] FAIL: zero bars downloaded", file=sys.stderr)
                failed += 1
                continue
            span = (bars[-1]["time"] - bars[0]["time"]) / 86400.0
            out_file = args.dest_dir / f"{sym}_{args.timeframe}.json"
            out_file.write_text(json.dumps(bars))
            print(f"[{sym}] SAVED: {len(bars)} bars ({span:.1f} days) -> {out_file}\n")
        except Exception as exc:
            print(f"[{sym}] ERROR: {exc}", file=sys.stderr)
            failed += 1

    return 1 if failed == len(symbols) else 0


if __name__ == "__main__":
    raise SystemExit(main())
