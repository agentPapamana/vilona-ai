#!/usr/bin/env python3
"""Validate candle price precision against broker tick size, and report it.

The M5 fetchers previously wrote broker bars without checking that printed
prices agree with the symbol's tick size. EURUSD bars with two decimals and a
`0.00001` tick slipped through and produced a strategy conclusion from quantized
data. This module is the single place that decides whether a bar set is
trustworthy for research.

Usage:
    python scripts/candle_quality.py data/candles/symbol_info.json data/candles_m5_verified/XAUUSD_5m.json
    python scripts/candle_quality.py --dir data/candles_m5_verified
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PRICE_FIELDS = ("open", "high", "low", "close")
MIN_DISTINCT_TICKS = 100
MIN_SANE_SHARE = 0.995
MAX_COARSER_SHARE = 0.98


def tick_decimals(tick_size: float) -> int:
    """Decimal places needed to print a price on this tick grid."""
    text = f"{tick_size:.10f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def precision_report(bars: list[dict], tick_size: float) -> dict:
    """Return outcome counts for bars printed off the tick grid.

    A bar is `aligned` when every OHLC price is an exact multiple of the tick
    size. Alignment can still be satisfied by a coarser feed that rounds onto
    whole ticks, so `coarser_share` also counts bars that never print the finest
    digit. `sane` requires a consistent OHLC, so a quantized feed is reported
    separately from a structurally broken bar.
    """
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    decimals = tick_decimals(tick_size)
    aligned = ordered = coarse = 0
    distinct: set[int] = set()
    for bar in bars:
        prices = [float(bar[field]) for field in PRICE_FIELDS]
        o, h, l, c = prices
        ordered += int(h >= max(o, c) and l <= min(o, c))
        aligned += int(all(round(price / tick_size, 6) == round(price / tick_size)
                           for price in prices))
        coarse += int(all(round(price, decimals) == round(price, max(0, decimals - 1))
                          for price in prices))
        distinct.add(round(c / tick_size))
    return {
        "bars": len(bars),
        "distinct_close_ticks": len(distinct),
        "sane_share": ordered / len(bars) if bars else 0.0,
        "offgrid_share": 1.0 - (aligned / len(bars) if bars else 0.0),
        "coarser_share": coarse / len(bars) if bars else 0.0,
    }


def assert_usable(symbol: str, bars: list[dict], tick_size: float) -> None:
    """Raise ValueError when a bar set must not be used for research."""
    if not bars:
        raise ValueError(f"{symbol}: no bars")
    report = precision_report(bars, tick_size)
    if report["distinct_close_ticks"] < MIN_DISTINCT_TICKS:
        raise ValueError(
            f"{symbol}: only {report['distinct_close_ticks']} distinct close ticks across "
            f"{report['bars']} bars at tick_size {tick_size}; feed is quantized"
        )
    if report["offgrid_share"] > 0:
        raise ValueError(
            f"{symbol}: {report['offgrid_share'] * 100:.1f}% of bars print prices off the "
            f"tick grid (tick_size {tick_size}); serialization loses precision"
        )
    if report["coarser_share"] > MAX_COARSER_SHARE:
        raise ValueError(
            f"{symbol}: {report['coarser_share'] * 100:.1f}% of bars never print a "
            f"{tick_decimals(tick_size)}-digit price; feed is coarser than tick_size {tick_size}"
        )
    if report["sane_share"] < MIN_SANE_SHARE:
        raise ValueError(
            f"{symbol}: only {report['sane_share'] * 100:.1f}% of bars have ordered OHLC"
        )


def _iter_snapshots(directory: Path):
    info_path = directory / "symbol_info.json"
    info = json.loads(info_path.read_text())
    for path in sorted(directory.glob("*_5m.json")) + sorted(directory.glob("*_15m.json")):
        symbol, timeframe = path.stem.rsplit("_", 1)
        yield symbol, timeframe, path, info.get(symbol)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshots", nargs="*", type=Path, help="candle JSON files")
    ap.add_argument("--dir", type=Path, help="directory holding *_{5m,15m}.json + symbol_info.json")
    args = ap.parse_args()
    targets = []
    if args.dir:
        targets.extend(_iter_snapshots(args.dir))
    for path in args.snapshots:
        info = json.loads((path.parent / "symbol_info.json").read_text()).get(path.stem.split("_")[0])
        targets.append((path.stem.split("_")[0], path.stem.rsplit("_", 1)[1], path, info))
    if not targets:
        ap.error("no snapshots given")
    failures = 0
    for symbol, timeframe, path, metadata in targets:
        if not metadata:
            print(f"{symbol:7s} {timeframe:4s} MISSING broker metadata")
            failures += 1
            continue
        bars = json.loads(path.read_text())
        try:
            assert_usable(symbol, bars, float(metadata["tick_size"]))
        except (KeyError, TypeError, ValueError) as exc:
            print(f"{symbol:7s} {timeframe:4s} REJECTED — {exc}")
            failures += 1
            continue
        report = precision_report(bars, float(metadata["tick_size"]))
        print(f"{symbol:7s} {timeframe:4s} OK bars={report['bars']} "
              f"ticks={report['distinct_close_ticks']} ordered={report['sane_share']:.3f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
