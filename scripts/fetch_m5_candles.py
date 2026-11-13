"""Fetch M5 candles and reject unverifiable symbol histories."""
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
PAGE_SECONDS = 7 * 86400
FIELDS = ("time", "open", "high", "low", "close", "tick_volume")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=190)
    parser.add_argument("--out", type=Path, default=Path("data/candles_m5"))
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    bridge = MT5MCPBridge(args.url, timeout=args.timeout)
    if not bridge.connect():
        print("bridge unavailable", file=sys.stderr)
        return 1

    metadata: dict[str, dict] = {}
    failures = 0
    now = int(time.time())
    for symbol in (s.strip().upper() for s in args.symbols.split(",") if s.strip()):
        raw = bridge.get_symbol_info(symbol)
        if raw is None:
            print(f"{symbol}: no broker symbol metadata", file=sys.stderr)
            failures += 1
            continue
        cursor = now - args.days * 86400
        bars: dict[int, dict] = {}
        while cursor < now:
            page_end = min(cursor + PAGE_SECONDS, now)
            for bar in bridge.get_candles_range(symbol, "5m", cursor, page_end, limit=2500) or []:
                if isinstance(bar, dict) and "time" in bar:
                    bars[int(bar["time"])] = {key: bar.get(key) for key in FIELDS}
            cursor = page_end
        candles = [bars[t] for t in sorted(bars)]
        span_days = (candles[-1]["time"] - candles[0]["time"]) / 86400 if len(candles) >= 2 else 0
        try:
            if span_days < args.days * 0.95:
                raise ValueError(f"only {span_days:.1f} days, requested {args.days}")
            assert_usable(symbol, candles, float(raw.tick_size))
        except (TypeError, ValueError) as exc:
            print(f"{symbol}: REJECTED — {exc}", file=sys.stderr)
            failures += 1
            continue
        metadata[symbol] = {
            "tick_value": raw.tick_value, "tick_size": raw.tick_size,
            "lot_step": raw.lot_step, "lot_min": raw.lot_min,
            "lot_max": raw.lot_max, "point": raw.point, "digits": raw.digits,
        }
        path = args.out / f"{symbol}_5m.json"
        path.write_text(json.dumps(candles, separators=(",", ":")))
        print(f"{symbol}: {len(candles)} verified bars, {span_days:.1f} days -> {path}")

    metadata_path = args.out / "symbol_info.json"
    existing = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    existing.update(metadata)
    metadata_path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
