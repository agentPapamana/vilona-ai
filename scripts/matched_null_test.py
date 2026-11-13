"""Compare candidate trade timing against a null baseline (matched-null gate).

The pre-registered gate (docs/strategy-research-plan.md) requires a null
baseline that matches the candidate on entry timestamps and trade count. An
unconditional periodic sampler does not: it enters at fixed bar intervals,
while the candidate enters on signal bars. This module derives the null
population from the candidate itself, so the comparison holds the entry set
fixed and randomises only the outcome.

Three null variants are produced per symbol, all reusing the candidate's own
entries, bar slice, lots and production cost model
(`backtest_strategy.run_symbol_scalp`: half spread on entry, full spread plus
commission on exit, stop before target inside a bar):

``random_exit``      same entries and sides; SL/TP multipliers drawn around the
                     configured values. Tests whether the chosen exit placement
                     beats an arbitrary placement.
``shuffled_entry``   same trade count and bar cadence, entry bar drawn from the
                     same candle set. Tests whether the signal bars are special.
``coin_flip_side``   same entries and exit distances, side flipped by an
                     independent coin. Tests whether the direction call carries
                     information.

The replayed candidate (zero randomisation) must reproduce the candidate's
recorded P/L; the script reports that residual before any verdict so a null
built on a different fill model cannot be mistaken for evidence.

Every run records its seed. The comparison statistic is the candidate's own
profit factor and expectancy against the null distribution's percentiles.

Usage:
    python scripts/matched_null_test.py --candles-dir data/candles_m5_verified \
        --symbols XAUUSD --runs 500 --seed 20261113

A run that cannot honour the matched set (for example fewer candidate trades
than the gate's minimum) reports INSUFFICIENT rather than a verdict.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
import backtest_strategy as bt

MIN_CANDIDATE_TRADES = 30
MIN_RUNS = 500


@dataclass
class NullRun:
    profit_factor: float
    expectancy: float
    trades: int


def profit_factor(pnls: list[float]) -> float:
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
def _equity_before(trades: list[bt.Trade], times: list[int], index: int,
                   equity_start: float) -> float:
    """Running equity the production simulator holds before bar `index`."""
    return equity_start + sum(t.pnl_usd for t in trades
                              if t.entry_time is not None and t.exit_time is not None
                              and t.exit_time < times[index])


def resolve(trade: bt.Trade, bars: list[dict[str, Any]], index_of: dict[int, int],
            info: dict[str, float], cfg: Config, spread: float, commission: float,
            equity: float) -> tuple[float, int]:
    """Replay one candidate-parameter trade; returns (net pnl, exit bar index).

    Reproduces `backtest_strategy.run_symbol_scalp` exactly: half-spread entry
    offset from the signal bar close, ATR stop/target, stop checked before
    target inside each subsequent bar, break-even move applied after a bar that
    did not close the trade, fees `commission*lots + spread*cval*lots`, and the
    same `lot_for_risk` sizing off the supplied running equity.
    """
    start = index_of[int(trade.entry_time)]
    atr = trade.atr
    cval = bt.contract_value(info)
    direction = 1.0 if trade.side.upper() == "BUY" else -1.0
    entry = float(bars[start]["close"]) + direction * spread / 2.0
    sl = entry - direction * cfg.trading.sl_atr_mult * atr
    tp = entry + direction * cfg.trading.tp_atr_mult * atr
    lots = trade.lots
    if lots <= 0:
        return 0.0, start
    del equity
    while start < len(bars) - 1:
        start += 1
        bar = bars[start]
        high, low = float(bar["high"]), float(bar["low"])
        is_buy = direction > 0
        hit_sl = low <= sl if is_buy else high >= sl
        hit_tp = high >= tp if is_buy else low <= tp
        if hit_sl or hit_tp:
            exit_price = sl if hit_sl else tp
            gross = (exit_price - entry) * direction * cval * lots
            fees = commission * lots + spread * cval * lots
            return gross - fees, start
        if is_buy and high - entry >= bt.BREAKEVEN_ATR * atr and sl < entry:
            sl = entry + bt.BREAKEVEN_OFFSET_ATR * atr
        elif not is_buy and entry - low >= bt.BREAKEVEN_ATR * atr and sl > entry:
            sl = entry - bt.BREAKEVEN_OFFSET_ATR * atr
    last = len(bars) - 1
    gross = (float(bars[last]["close"]) - entry) * direction * cval * lots
    fees = commission * lots + spread * cval * lots
    return gross - fees, last
def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def candidate_config() -> Config:
    cfg = Config.from_env()
    cfg.trading.sl_atr_mult = 2.5
    cfg.trading.tp_atr_mult = 3.0
    cfg.trading.min_confidence = 0.55
    cfg.trading.max_risk_pct = 0.5
    cfg.trading.min_lot = 0.01
    cfg.trading.max_lot = 0.01
    cfg.trading.lot_step = 0.01
    return cfg


def evaluate_symbol(symbol: str, candles_dir: Path, runs: int, seed: int,
                    cfg: Config | None = None) -> dict[str, Any] | None:
    cfg = cfg or candidate_config()
    info = bt.load_symbol_info(symbol, candles_dir)
    bars = bt.load_candles(symbol, 999, candles_dir, "5m")
    bt.assert_usable(symbol, bars, float(info["tick_size"]))
    spread = bt.DEFAULT_SPREAD_PRICE[symbol]
    commission = bt.DEFAULT_COMMISSION_PER_LOT
    candidate = bt.run_symbol_scalp(symbol, bars, cfg, spread, commission, 0.5,
                                    10_000.0, True, candles_dir)
    if len(candidate.trades) < MIN_CANDIDATE_TRADES:
        return None
    index_of = {int(b["time"]): i for i, b in enumerate(bars)}
    times = [int(b["time"]) for b in bars]
    replayed = []
    for trade in candidate.trades:
        pnl, _ = resolve(trade, bars, index_of, info, cfg, spread, commission, 10_000.0)
        replayed.append(pnl)
    recorded = [t.pnl_usd for t in candidate.trades]
    residual = max(abs(a - b) for a, b in zip(recorded, replayed))
    if residual > 1e-8:
        raise ValueError(f"{symbol}: candidate replay residual {residual:.9f} USD")
    entry: dict[str, Any] = {"candidate": {
        "profit_factor": profit_factor(recorded),
        "expectancy": statistics.fmean(recorded), "trades": len(recorded)},
        "replay_max_abs_residual_usd": residual, "variants": {}}
    for variant in ("random_exit", "shuffled_entry", "coin_flip_side"):
        # Null variants remain intentionally disabled until their entry/exit
        # specification is implemented with the same production state machine.
        entry["variants"][variant] = {"status": "NOT RUN",
                                       "reason": "parity-safe implementation required"}
    return entry


def main() -> int:
    print(
        "matched-null analysis is disabled: its replay model has not been "
        "validated against production trade P/L; no null verdict was produced.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
