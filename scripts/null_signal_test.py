#!/usr/bin/env python3
"""Synthetic null test: does a random signal beat slice-and-dice on real bars?

Recommendation step A5 from the Phase 4 review. If a coin-flip entry with the
same stops, costs and sizing cannot be distinguished from the tested rules, the
rules have no measurable edge. A working harness should return a random-entry
profit factor around 0.8-0.9 after spread and commission.

Each run picks a random side at every Nth bar, uses the same ATR stop and target
from `config.TradingConfig`, the same spread/commission model and the same
lot sizing as `scripts/backtest_strategy.py`.

Usage:
    python scripts/null_signal_test.py --candles-dir-m5 data/candles_m5_verified \
        --symbols XAUUSD --runs 200 --period 50
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_strategy import (
    BREAKEVEN_ATR, BREAKEVEN_OFFSET_ATR, DEFAULT_COMMISSION_PER_LOT, DEFAULT_SPREAD_PRICE,
    contract_value, load_symbol_info, lot_for_risk, summarize, Trade, SymbolResult,
)
from config import Config
from core.quant_scanner import compute_atr


def null_run(symbol, bars, cfg, spread, commission, risk_pct, equity_start, period, rng, candles_dir):
    info = load_symbol_info(symbol, candles_dir)
    cval = contract_value(info)
    sl_mult, tp_mult = cfg.trading.sl_atr_mult, cfg.trading.tp_atr_mult
    equity, open_trade, trades = equity_start, None, []
    next_entry = 60
    for i in range(60, len(bars) - 1):
        bar = bars[i]
        atr = compute_atr(bars[i - 60:i + 1], 14)
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
                gross = (open_trade.exit - open_trade.entry) * direction * cval * open_trade.lots
                fees = (commission + spread * cval) * open_trade.lots
                open_trade.fees_usd = fees
                open_trade.pnl_usd = gross - fees
                equity += open_trade.pnl_usd
                trades.append(open_trade)
                open_trade = None
                continue
            if is_buy and hi - open_trade.entry >= BREAKEVEN_ATR * open_trade.atr \
                    and open_trade.sl < open_trade.entry:
                open_trade.sl = open_trade.entry + BREAKEVEN_OFFSET_ATR * open_trade.atr
            elif not is_buy and open_trade.entry - lo >= BREAKEVEN_ATR * open_trade.atr \
                    and open_trade.sl > open_trade.entry:
                open_trade.sl = open_trade.entry - BREAKEVEN_OFFSET_ATR * open_trade.atr
            continue
        if atr <= 0 or i < next_entry:
            continue
        next_entry = i + period
        buy = rng.random() < 0.5
        close = float(bar["close"])
        entry = close + (spread / 2 if buy else -spread / 2)
        sl = entry - sl_mult * atr if buy else entry + sl_mult * atr
        tp = entry + tp_mult * atr if buy else entry - tp_mult * atr
        lots = lot_for_risk(equity * (risk_pct / 100), abs(entry - sl), cval, info, cfg)
        if lots <= 0:
            continue
        open_trade = Trade(symbol, "BUY" if buy else "SELL", int(bar["time"]),
                           entry, sl, tp, atr, lots)
    return SymbolResult(symbol, len(bars), 0.0, trades=trades)


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, default=Path("data/candles_m5_verified"))
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--symbols", default="XAUUSD,USDJPY")
    ap.add_argument("--period", type=int, default=50, help="bars between random entries")
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--commission", type=float, default=DEFAULT_COMMISSION_PER_LOT)
    ap.add_argument("--risk-pct", type=float, default=0.5)
    ap.add_argument("--equity", type=float, default=10000.0)
    args = ap.parse_args()
    cfg = Config.from_env()
    rng = random.Random(20261112)
    for symbol in (s.strip().upper() for s in args.symbols.split(",") if s.strip()):
        path = args.dir / f"{symbol}_{args.timeframe}.json"
        bars = json.loads(path.read_text())
        spread = DEFAULT_SPREAD_PRICE[symbol]
        factors, expectancies, trade_counts = [], [], []
        for _ in range(args.runs):
            stats = summarize([null_run(symbol, bars, cfg, spread, args.commission,
                                        args.risk_pct, args.equity, args.period, rng, args.dir)])
            factors.append(stats["profit_factor"])
            expectancies.append(stats["expectancy_usd"])
            trade_counts.append(stats["trades"])
        print(f"{symbol}: runs={args.runs} period={args.period} trades/run median={statistics.median(trade_counts):.0f}")
        print(f"  PF p05={percentile(factors, 0.05):.3f} median={statistics.median(factors):.3f} "
              f"p95={percentile(factors, 0.95):.3f}")
        print(f"  expectancy median=${statistics.median(expectancies):.2f} "
              f"p05=${percentile(expectancies, 0.05):.2f} p95=${percentile(expectancies, 0.95):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
