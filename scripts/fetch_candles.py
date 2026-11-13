"""Fetch a long M15 candle snapshot for the B9 backtest gate.

The bridge tail read (`get_candles`) is capped at 500 bars by the EA, roughly
seven days on M15. This script pages the dated `get_candles_range` command
instead and writes the offline snapshot layout that
`scripts/backtest_strategy.py --candles-dir` expects:

    <out>/<SYMBOL>_15m.json     [{"time","open","high","low","close","tick_volume"}, ...]
    <out>/symbol_info.json      {symbol: {tick_value, tick_size, lot_step, ...}}

Usage:
    python scripts/fetch_candles.py --days 190
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from candle_quality import assert_usable
from trading.bridge_mt5_mcp import MT5MCPBridge

SYMBOLS = ("XAUUSD", "EURUSD", "USDJPY")
PAGE_SECONDS = 7 * 24 * 3600
BAR_SECONDS = 900
BARS_PER_PAGE = PAGE_SECONDS // BAR_SECONDS
FIELDS = ("time", "open", "high", "low", "close", "tick_volume")


def fetch_validated(bridge: MT5MCPBridge, symbol: str, timeframe: str, days: int) -> tuple[list[dict], dict]:
    """Fetch one symbol and reject it unless metadata and precision verify."""
    raw = bridge.get_symbol_info(symbol)
    if raw is None:
        raise ValueError("no symbol metadata")
    if raw.tick_size <= 0 or raw.tick_value <= 0:
        raise ValueError(f"invalid tick metadata: tick_size={raw.tick_size} tick_value={raw.tick_value}")
    now = int(time.time())
    page_seconds = 7 * 86400
    per_page = page_seconds // BAR_SECONDS[timeframe]
    bars: dict[int, dict] = {}
    cursor = now - days * 86400
    while cursor < now:
        page_end = min(cursor + page_seconds, now)
        for bar in bridge.get_candles_range(symbol, timeframe, cursor, page_end, limit=per_page) or []:
            if isinstance(bar, dict) and "time" in bar:
                bars[int(bar["time"])] = {field: bar.get(field) for field in FIELDS}
        cursor = page_end
    candles = [bars[key] for key in sorted(bars)]
    span_days = (candles[-1]["time"] - candles[0]["time"]) / 86400.0 if len(candles) >= 2 else 0.0
    if span_days < days * 0.95:
        raise ValueError(f"only {span_days:.1f} days, requested {days}")
    assert_usable(symbol, candles, float(raw.tick_size))
    return candles, {
        "tick_value": raw.tick_value,
        "tick_size": raw.tick_size,
        "lot_step": raw.lot_step,
        "lot_min": raw.lot_min,
        "lot_max": raw.lot_max,
        "point": raw.point,
        "digits": raw.digits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=190)
    parser.add_argument("--timeframe", choices=BAR_SECONDS, default="15m")
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--out", type=Path, default=Path("data/candles"))
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    bridge = MT5MCPBridge(args.url, timeout=args.timeout)
    if not bridge.connect():
        print("cannot reach the MT5 bridge", file=sys.stderr)
        return 1

    info: dict[str, dict] = {}
    failures = 0
    for symbol in (s.strip().upper() for s in args.symbols.split(",") if s.strip()):
        try:
            candles, metadata = fetch_validated(bridge, symbol, args.timeframe, args.days)
        except (TypeError, ValueError) as exc:
            print(f"{symbol}: REJECTED — {exc}", file=sys.stderr)
            failures += 1
            continue
        info[symbol] = metadata
        path = args.out / f"{symbol}_{args.timeframe}.json"
        path.write_text(json.dumps(candles, separators=(",", ":")))
        span_days = (candles[-1]["time"] - candles[0]["time"]) / 86400.0
        print(f"{symbol}: {len(candles)} verified bars, {span_days:.1f} days -> {path}")

    metadata_path = args.out / "symbol_info.json"
    existing = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    existing.update(info)
    metadata_path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    return 1 if failures else 0



if __name__ == "__main__":
    raise SystemExit(main())
