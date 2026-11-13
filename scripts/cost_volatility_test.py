"""Experiment R1: does cost relative to volatility explain the strategy loss?

Pre-registered in `docs/strategy-research-plan.md` (week 1). It reuses the production
`scalp_5m` rule and fixed cost assumptions from `scripts/backtest_strategy.py`.
The fixed spread is only a diagnostic substitute; it cannot satisfy the research
gate's historical-spread requirement. For every entry the script records

    cost_ratio = spread / ATR(5m at entry)

so trades can be grouped by how large the transaction cost is relative to the
barrier the trade must clear. The question is narrow: if the loss disappears in
low-cost buckets and persists in high-cost buckets, the problem is cost; if every
bucket loses, the problem is the signal.

Modes are reported, never selected. The in-sample half is reported next to the
out-of-sample half of the *same* pre-declared buckets, so a bucket that only
looks healthy after splitting is visible as a choice, not a result. This script
cannot pass the B9 gate: it reports a descriptive breakdown and always exits 0
unless the input data is unusable.

Usage:
    python scripts/cost_volatility_test.py --candles-dir-m5 data/candles_m5_verified \
        --symbols XAUUSD --months 6
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from core.quant_scanner import (
    compute_atr, compute_ema, compute_scalp_5m_signal, compute_setup_confidence,
)
from candle_quality import assert_usable
from backtest_strategy import (
    DEFAULT_COMMISSION_PER_LOT, DEFAULT_SPREAD_PRICE, SymbolResult, Trade,
    contract_value, load_symbol_info, lot_for_risk, resample, summarize,
)

# Declared before looking at any result. Edit only with a new entry in the
# evidence log; moving these after a run is how a screen turns into a fit.
COST_RATIO_EDGES = (0.10, 0.15, 0.20, 0.25, 0.33)
MIN_BUCKET_TRADES = 30

def bucket_label(ratio: float) -> str:
    low = 0.0
    for edge in COST_RATIO_EDGES:
        if ratio < edge:
            return f"[{low:.2f},{edge:.2f})"
        low = edge
    return f"[{low:.2f},inf)"


def simulate(symbol: str, bars: list[dict[str, Any]], cfg: Config, spread: float,
             commission_per_lot: float, candles_dir: Path) -> SymbolResult:
    """Run the production scalp_5m rule and tag every trade with its cost ratio."""
    info = load_symbol_info(symbol, candles_dir)
    cval = contract_value(info)
    c15m = resample(bars, 900)
    bucket_index: dict[int, int] = {}
    cursor = -1
    for bar in bars:
        bucket = int(bar["time"]) // 900 * 900
        if bucket in bucket_index:
            continue
        while cursor + 1 < len(c15m) and int(c15m[cursor + 1]["time"]) <= bucket:
            cursor += 1
        bucket_index[bucket] = cursor

    sl_mult = cfg.trading.sl_atr_mult
    tp_mult = cfg.trading.tp_atr_mult
    band = float(getattr(cfg.trading, "scalp_pullback_band", 1.5))
    min_conf = cfg.trading.min_confidence
    risk_pct = 0.5
    result = SymbolResult(symbol, len(bars), 0.0)
    equity, open_trade, last_bucket = 10_000.0, None, -1
    for i in range(60, len(bars) - 1):
        bar = bars[i]
        if open_trade is not None:
            nxt = bars[i + 1]
            hi, lo = float(nxt["high"]), float(nxt["low"])
            is_buy = open_trade.side == "BUY"
            hit_sl = lo <= open_trade.sl if is_buy else hi >= open_trade.sl
            hit_tp = hi >= open_trade.tp if is_buy else lo <= open_trade.tp
            if hit_sl or hit_tp:
                open_trade.exit = open_trade.sl if hit_sl else open_trade.tp
                open_trade.reason = "sl" if hit_sl else "tp"
                open_trade.exit_time = int(nxt["time"])
                direction = 1.0 if is_buy else -1.0
                fees = commission_per_lot * open_trade.lots + spread * cval * open_trade.lots
                open_trade.fees_usd = fees
                open_trade.pnl_usd = (
                    (open_trade.exit - open_trade.entry) * direction * cval * open_trade.lots
                ) - fees
                equity += open_trade.pnl_usd
                result.trades.append(open_trade)
                open_trade = None
                continue
            # Same break-even move as the production backtest.
            if is_buy and hi - open_trade.entry >= open_trade.atr and open_trade.sl < open_trade.entry:
                open_trade.sl = open_trade.entry + 0.1 * open_trade.atr
            elif not is_buy and open_trade.entry - lo >= open_trade.atr and open_trade.sl > open_trade.entry:
                open_trade.sl = open_trade.entry - 0.1 * open_trade.atr
            continue
        bucket = int(bar["time"]) // 900 * 900
        if bucket == last_bucket:
            continue
        end = bucket_index.get(bucket, -1)
        if end < 49:
            continue
        c15_window = c15m[end - 59:end + 1]
        c5_window = bars[i - 19:i + 1]
        price = float(bar["close"])
        action, _conf, _reason, atr = compute_scalp_5m_signal(c15_window, c5_window, price, band)
        if action == "WAIT" or atr <= 0:
            continue
        atr15 = compute_atr(c15_window, 14)
        if atr15 <= 0:
            continue
        atr_series = [v for v in (compute_atr(c15_window[:k + 1], 14)
                                  for k in range(len(c15_window) - 1,
                                                  max(len(c15_window) - 31, 14), -1))
                      if v > 0]
        closes = [float(b["close"]) for b in c15_window]
        confidence = compute_setup_confidence(
            compute_ema(closes, 21), compute_ema(closes, 50), price, atr15, atr_series,
            action == "BUY")
        if confidence < min_conf:
            continue
        bullish = action == "BUY"
        entry = price + (spread / 2.0 if bullish else -spread / 2.0)
        sl = entry - sl_mult * atr if bullish else entry + sl_mult * atr
        tp = entry + tp_mult * atr if bullish else entry - tp_mult * atr
        lots = lot_for_risk(equity * (risk_pct / 100.0), abs(entry - sl), cval, info, cfg)
        if lots <= 0:
            continue
        result.signals += 1
        last_bucket = bucket
        open_trade = Trade(symbol=symbol, side=action, entry_time=int(bar["time"]),
                           entry=entry, sl=sl, tp=tp, atr=atr, lots=lots)
        open_trade.cost_ratio = spread / atr
    return result


def _stats(trades: list[Trade]) -> dict[str, Any]:
    summary = summarize([SymbolResult("bucket", 0, 0.0, trades=trades)])
    summary["median_cost_ratio"] = (
        statistics.median(t.cost_ratio for t in trades) if trades else 0.0
    )
    summary["sufficient"] = summary["trades"] >= MIN_BUCKET_TRADES
    return summary

def report(symbol: str, trades: list[Trade], split_time: int) -> dict[str, Any]:
    labels = [bucket_label(ratio) for ratio in COST_RATIO_EDGES] + [bucket_label(1.0)]
    labels = sorted(set(labels))
    rows: dict[str, Any] = {}
    for label in labels + ["all"]:
        subset = trades if label == "all" else [t for t in trades if bucket_label(t.cost_ratio) == label]
        oos = [t for t in subset if t.entry_time >= split_time]
        rows[label] = {"full": _stats(subset), "oos": _stats(oos)}
    return {"symbol": symbol, "rows": rows}

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="XAUUSD")
    ap.add_argument("--candles-dir-m5", type=Path, default=Path("data/candles_m5_verified"))
    ap.add_argument("--candles-dir", type=Path, default=Path("data/candles"))
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--commission", type=float, default=DEFAULT_COMMISSION_PER_LOT)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    cfg = Config.from_env()
    payload, blocked = [], []
    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        path = args.candles_dir_m5 / f"{symbol}_5m.json"
        if not path.exists():
            blocked.append(f"{symbol}: missing {path}")
            continue
        bars = json.loads(path.read_text())
        try:
            assert_usable(symbol, bars, float(load_symbol_info(symbol, args.candles_dir)["tick_size"]))
        except (ValueError, KeyError) as exc:
            blocked.append(f"{symbol}: data rejected — {exc}")
            continue
        span_days = (bars[-1]["time"] - bars[0]["time"]) / 86400.0
        if span_days < args.months * 30:
            blocked.append(f"{symbol}: only {span_days:.0f} days, {args.months * 30} required")
            continue
        spread = DEFAULT_SPREAD_PRICE.get(symbol, 0.0)
        result = simulate(symbol, bars, cfg, spread, args.commission, args.candles_dir)
        if not result.trades:
            blocked.append(f"{symbol}: no trades produced")
            continue
        split_time = bars[len(bars) // 2]["time"]
        block = report(symbol, result.trades, split_time)
        block["span_days"] = span_days
        block["spread"] = spread
        payload.append(block)

    for block in payload:
        print(f"\n=== {block['symbol']} cost/volatility screen "
              f"(span={block['span_days']:.0f}d, spread={block['spread']}) ===")
        print(f"{'cost/ATR bucket':18s} {'n':>5s} {'exp$':>9s} {'PF':>7s} {'win%':>6s} "
              f"{'median':>7s} | {'OOS n':>5s} {'OOS exp$':>9s} {'OOS PF':>7s}")
        for label, row in block["rows"].items():
            full, oos = row["full"], row["oos"]
            flag = "" if label == "all" or full["sufficient"] else " (thin)"
            print(f"{label:18s} {full['trades']:5d} {full['expectancy_usd']:9.2f} "
                  f"{full['profit_factor']:7.3f} {full['win_rate']:6.1f} "
                  f"{full['median_cost_ratio']:7.3f} | {oos['trades']:5d} "
                  f"{oos['expectancy_usd']:9.2f} {oos['profit_factor']:7.3f}{flag}")
        print("  Buckets are declared before the run; a bucket that only looks healthy "
              "in one half is a hypothesis, not a candidate.")
    if blocked:
        print("\nNOT RUN (data or coverage):")
        for line in blocked:
            print(f"  - {line}")
    print("\nThis screen does not pass or fail the B9 gate. Record the output in the "
          "evidence log before drawing a conclusion.")
    if args.json_out is not None:
        args.json_out.write_text(json.dumps({"symbols": payload, "blocked": blocked}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
