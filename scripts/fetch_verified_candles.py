#!/usr/bin/env python3
"""Fetch a dated candle snapshot and reject price-quantized history.

Usage: python scripts/fetch_verified_candles.py --timeframe 5m --days 190
Writes the same {SYMBOL}_{timeframe}.json + symbol_info.json layout used by
scripts/backtest_strategy.py. Unlike the legacy fetch scripts, this fails when
prices do not vary at the broker's advertised tick size.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BAR_SECONDS = {"5m": 300, "15m": 900}
FIELDS = ("time", "open", "high", "low", "close", "tick_volume")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from candle_quality import assert_usable
from trading.bridge_mt5_mcp import MT5MCPBridge

SYMBOLS = ("XAUUSD", "EURUSD", "USDJPY")


def fetch(bridge: MT5MCPBridge, symbol: str, timeframe: str, days: int) -> list[dict]:
    now = int(time.time())
    start = now - days * 86400
    page_seconds = 7 * 86400
    per_page = page_seconds // BAR_SECONDS[timeframe]
    bars: dict[int, dict] = {}
    cursor = start
    while cursor < now:
        page_end = min(cursor + page_seconds, now)
        for bar in bridge.get_candles_range(symbol, timeframe, cursor, page_end, limit=per_page) or []:
            if isinstance(bar, dict) and "time" in bar:
                bars[int(bar["time"])] = {field: bar.get(field) for field in FIELDS}
        cursor = page_end
    return [bars[t] for t in sorted(bars)]


def validate_prices(symbol: str, bars: list[dict], tick_size: float) -> None:
    assert_usable(symbol, bars, tick_size)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeframe", choices=BAR_SECONDS, required=True)
    parser.add_argument("--days", type=int, default=190)
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    out_dir = args.out or Path("data/candles_m5" if args.timeframe == "5m" else "data/candles")
    out_dir.mkdir(parents=True, exist_ok=True)
    bridge = MT5MCPBridge(args.url, timeout=args.timeout)
    if not bridge.connect():
        print("cannot reach the MT5 bridge", file=sys.stderr)
        return 1

    info = {}
    failures = 0
    for symbol in (s.strip().upper() for s in args.symbols.split(",") if s.strip()):
        raw = bridge.get_symbol_info(symbol)
        if raw is None:
            print(f"{symbol}: no broker symbol metadata", file=sys.stderr)
            failures += 1
            continue
        metadata = {"tick_value": raw.tick_value, "tick_size": raw.tick_size,
                    "lot_step": raw.lot_step, "lot_min": raw.lot_min,
                    "lot_max": raw.lot_max, "point": raw.point}
        bars = fetch(bridge, symbol, args.timeframe, args.days)
        span = (bars[-1]["time"] - bars[0]["time"]) / 86400.0 if len(bars) >= 2 else 0.0
        try:
            validate_prices(symbol, bars, float(raw.tick_size))
            if span < args.days * 0.95:
                raise ValueError(f"only {span:.1f} days, requested {args.days}")
        except (TypeError, ValueError) as exc:
            print(f"{symbol}: REJECTED — {exc}", file=sys.stderr)
            failures += 1
            continue
        info[symbol] = metadata
        path = out_dir / f"{symbol}_{args.timeframe}.json"
        path.write_text(json.dumps(bars, separators=(",", ":")))
        print(f"{symbol}: {len(bars)} verified bars, {span:.1f} days -> {path}")
    metadata_path = out_dir / "symbol_info.json"
    existing = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    existing.update(info)
    metadata_path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
