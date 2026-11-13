"""Historical backtest for the deterministic strategy, used by the B9 gate.

Phase 4 gate (docs/blocker-checklist.md, B9): "run a backtest ... on historical
MT5 candles for XAUUSD, EURUSD, USDJPY (minimum 6 months, includes spread and
commission). Go to Phase 4 only if expectancy > 0 and profit factor > 1.1."

The `scalp_5m` option requires aligned M5 candles in `--candles-dir-m5`; the
backtest aggregates completed M5 bars into M15 trend candles.

1. Data sufficiency is checked first. A window shorter than the requested
   period exits with `DATA INSUFFICIENT` instead of reporting a verdict from a
   few days of candles.
2. Costs are charged on every trade at the traded volume: half the spread on
   entry (ask for BUY, bid for SELL), half on exit, plus commission per lot.
3. Position size uses the same risk model as `agents/risk_manager.py`:
   `lots = equity * risk_pct / per_lot_risk`, clamped to `min_lot`/`max_lot`,
   compounding with the running equity. Dollar figures therefore match live
   sizing; profit factor and expectancy per trade do not assume one full lot.

The signal rule is selectable because the B9 strategy rule is what the gate
measures:

    --strategy pullback   live QuantScanner rule: 1h EMA21/EMA50 agreement and a
                          15m pullback to EMA21 within a 1.2 ATR band.
    --strategy breakout   enter on a 20-bar 15m high/low break while the 1h
                          EMA21/EMA50 agreement holds.

Both variants share SL/TP placement, the optional live break-even move
(`core/quant_scanner.py`: at +1.0 ATR move SL to entry +/- 0.1 ATR), and
one position per symbol at a time.

The `scalp_5m` strategy follows `compute_scalp_5m_signal`: 15m EMA21/EMA50
direction plus a 5m EMA21 pullback trigger with ATR(5m). It uses the configured
SL/TP ATR multipliers and the same spread/commission model, and reads M5 candles
from `--candles-dir-m5`.

Usage:
    python scripts/backtest_strategy.py --months 6 --candles-dir data/candles
    python scripts/backtest_strategy.py --months 6 --candles-dir data/candles --strategy breakout
    python scripts/backtest_strategy.py --months 6 --candles-dir-m5 data/candles_m5 --strategy scalp_5m --walk-forward
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config
from core.quant_scanner import (
    compute_atr, compute_ema, compute_scalp_5m_signal, compute_setup_confidence,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candle_quality import assert_usable
SYMBOLS = ("XAUUSD", "EURUSD", "USDJPY")
BAR_SECONDS = 900
BARS_PER_MONTH = 30 * 24 * 4
DEFAULT_SPREAD_PRICE = {"XAUUSD": 0.30, "EURUSD": 0.00012, "USDJPY": 0.014}
DEFAULT_COMMISSION_PER_LOT = 7.0
BREAKEVEN_ATR = 1.0
BREAKEVEN_OFFSET_ATR = 0.1

@dataclass
class Trade:
    symbol: str
    side: str
    entry_time: int
    entry: float
    sl: float
    tp: float
    atr: float
    lots: float
    exit_time: int = 0
    exit: float = 0.0
    reason: str = ""
    pnl_usd: float = 0.0
    fees_usd: float = 0.0

@dataclass
class SymbolResult:
    symbol: str
    bars: int
    span_days: float
    signals: int = 0
    trades: list[Trade] = field(default_factory=list)

def load_symbol_info(symbol: str, candles_dir: Path | None, bridge=None) -> dict[str, float]:
    if candles_dir is not None:
        path = candles_dir / "symbol_info.json"
        if not path.exists(): raise FileNotFoundError(f"missing broker metadata: {path}")
        info = json.loads(path.read_text()).get(symbol)
        if not info: raise KeyError(f"{symbol} missing from broker metadata {path}")
    else:
        if bridge is None:
            from trading.bridge_mt5_mcp import MT5MCPBridge
            bridge = MT5MCPBridge("http://127.0.0.1:8765", timeout=60)
            if not bridge.connect(): raise RuntimeError("cannot reach the MT5 bridge")
        raw = bridge.get_symbol_info(symbol)
        if raw is None or raw.tick_size <= 0 or raw.tick_value <= 0: raise RuntimeError(f"{symbol}: invalid tick metadata")
        info = {"tick_value":raw.tick_value,"tick_size":raw.tick_size,"lot_step":raw.lot_step,"lot_min":raw.lot_min,"lot_max":raw.lot_max,"point":raw.point}
    if float(info.get("tick_size",0)) <= 0 or float(info.get("tick_value",0)) <= 0: raise ValueError(f"{symbol}: invalid tick metadata")
    return info

def contract_value(info: dict[str,float]) -> float:
    return float(info["tick_value"])/float(info["tick_size"])

def lot_for_risk(risk_usd:float, sl_distance:float, cval:float, info:dict[str,float], cfg:Config)->float:
    per_lot_risk=sl_distance*cval
    if per_lot_risk<=0:return 0.0
    raw=risk_usd/per_lot_risk;step=float(info["lot_step"]);minimum=float(info["lot_min"]);maximum=min(float(info["lot_max"]),cfg.trading.max_lot)
    lots=round(math.floor(raw/step)*step,4)
    return max(minimum,min(lots,maximum))

def load_candles(symbol: str, months: int, candles_dir: Path | None, timeframe="15m") -> list[dict[str, Any]]:
    if candles_dir is not None:
        path = candles_dir / f"{symbol}_{timeframe}.json"
        if not path.exists():
            raise FileNotFoundError(f"missing candle file: {path}")
        bars = json.loads(path.read_text())
        info = load_symbol_info(symbol, candles_dir)
        assert_usable(symbol, bars, float(info["tick_size"]))
        return bars
    from trading.bridge_mt5_mcp import MT5MCPBridge
    bridge = MT5MCPBridge("http://127.0.0.1:8765", timeout=60)
    if not bridge.connect():
        raise RuntimeError("cannot reach the MT5 bridge")
    return bridge.get_candles(symbol, timeframe, limit=months * BARS_PER_MONTH)

def ema_series_1h(candles:list[dict[str,Any]])->dict[int,tuple[float,float]]:
    buckets:dict[int,list[float]]={}
    for bar in candles:buckets.setdefault(int(bar["time"])//3600*3600,[]).append(float(bar["close"]))
    out={};closes=[]
    for bucket in sorted(buckets):
        closes.append(buckets[bucket][-1]);e21=compute_ema(closes,21) if len(closes)>=21 else 0.;e50=compute_ema(closes,50) if len(closes)>=50 else 0.;out[bucket]=(e21,e50)
    return out

def run_symbol(symbol,candles,cfg,spread,commission,risk_pct,equity_start,strategy="pullback",breakeven=True,candles_dir=None):
    span_days=(candles[-1]["time"]-candles[0]["time"])/86400. if candles else 0.;result=SymbolResult(symbol,len(candles),span_days)
    h1=ema_series_1h(candles);atr_history=[];equity=equity_start;open_trade=None;info=load_symbol_info(symbol,candles_dir);cval=contract_value(info);sl_mult=cfg.trading.sl_atr_mult;tp_mult=cfg.trading.tp_atr_mult;min_conf=cfg.trading.min_confidence
    for i in range(60,len(candles)-1):
        window=candles[i-60:i+1];bar=candles[i];atr=compute_atr(window,14)
        if atr>0:atr_history.append(atr)
        trend=h1.get(int(bar["time"])//3600*3600)
        if open_trade is not None:
            nxt=candles[i+1];hi,lo=float(nxt["high"]),float(nxt["low"]);is_buy=open_trade.side=="BUY";hit_sl=lo<=open_trade.sl if is_buy else hi>=open_trade.sl;hit_tp=hi>=open_trade.tp if is_buy else lo<=open_trade.tp
            if hit_sl or hit_tp:
                open_trade.exit=open_trade.sl if hit_sl else open_trade.tp;open_trade.reason="sl" if hit_sl else "tp";open_trade.exit_time=int(nxt["time"]);direction=1. if is_buy else -1.;gross=(open_trade.exit-open_trade.entry)*direction*cval*open_trade.lots;fees=commission*open_trade.lots+spread*cval*open_trade.lots;open_trade.fees_usd=fees;open_trade.pnl_usd=gross-fees;equity+=open_trade.pnl_usd;result.trades.append(open_trade);open_trade=None;continue
            if breakeven:
                if is_buy and hi-open_trade.entry>=BREAKEVEN_ATR*open_trade.atr and open_trade.sl<open_trade.entry:open_trade.sl=open_trade.entry+BREAKEVEN_OFFSET_ATR*open_trade.atr
                elif not is_buy and open_trade.entry-lo>=BREAKEVEN_ATR*open_trade.atr and open_trade.sl>open_trade.entry:open_trade.sl=open_trade.entry-BREAKEVEN_OFFSET_ATR*open_trade.atr
            continue
        if trend is None or atr<=0:continue
        ema21,ema50=trend
        if ema21<=0 or ema50<=0:continue
        close=float(bar["close"])
        if strategy=="pullback":
            bullish=close>ema21>ema50;bearish=close<ema21<ema50
            if not (bullish or bearish):continue
            distance=close-ema21 if bullish else ema21-close
            if distance<0 or distance>1.2*atr:continue
        elif strategy=="breakout":
            hh=max(float(c["high"]) for c in candles[i-20:i]);ll=min(float(c["low"]) for c in candles[i-20:i]);bullish=close>hh and ema21>ema50;bearish=close<ll and ema21<ema50
            if not (bullish or bearish):continue
        else:raise ValueError(f"unknown strategy: {strategy}")
        confidence=compute_setup_confidence(ema21,ema50,close,atr,atr_history[-50:],bullish)
        if confidence<min_conf:continue
        entry=close+(spread/2 if bullish else -spread/2);sl=entry-sl_mult*atr if bullish else entry+sl_mult*atr;tp=entry+tp_mult*atr if bullish else entry-tp_mult*atr;lots=lot_for_risk(equity*(risk_pct/100),abs(entry-sl),cval,info,cfg)
        if lots<=0:continue
        result.signals+=1;open_trade=Trade(symbol,"BUY" if bullish else "SELL",int(bar["time"]),entry,sl,tp,atr,lots)
    return result

def summarize(results):
    trades=[t for r in results for t in r.trades]
    if not trades:return {"trades":0,"expectancy_usd":0.,"profit_factor":0.,"win_rate":0.,"max_drawdown_usd":0.,"r_multiples":[],"fees_usd":0.}
    pnls=[t.pnl_usd for t in trades];gains=sum(p for p in pnls if p>0);losses=-sum(p for p in pnls if p<0);equity=0.;peak=0.;max_dd=0.
    for p in pnls:equity+=p;peak=max(peak,equity);max_dd=max(max_dd,peak-equity)
    r_multiples=[t.pnl_usd/(abs(t.entry-t.sl)*t.lots*1.0) if t.lots else 0. for t in trades]
    return {"trades":len(trades),"expectancy_usd":statistics.fmean(pnls),"profit_factor":gains/losses if losses else (float("inf") if gains else 0.),"win_rate":100*sum(p>0 for p in pnls)/len(pnls),"max_drawdown_usd":max_dd,"r_multiples":r_multiples,"fees_usd":sum(t.fees_usd for t in trades)}



def resample(candles: list[dict[str, Any]], seconds: int) -> list[dict[str, Any]]:
    """Aggregate bars into `seconds` buckets (300 -> 900 is M5 -> M15)."""
    buckets: dict[int, list[dict[str, Any]]] = {}
    for bar in candles:
        buckets.setdefault(int(bar["time"]) // seconds * seconds, []).append(bar)
    return [
        {
            "time": start,
            "open": float(buckets[start][0]["open"]),
            "high": max(float(b["high"]) for b in buckets[start]),
            "low": min(float(b["low"]) for b in buckets[start]),
            "close": float(buckets[start][-1]["close"]),
        }
        for start in sorted(buckets)
    ]


def run_symbol_scalp(symbol, candles_5m, cfg, spread, commission, risk_pct, equity_start, breakeven=True, candles_dir=None):
    """Mirror the live scalp_5m path: 15m trend, 5m EMA21 pullback, ATR(5m) stops."""
    span_days = (candles_5m[-1]["time"] - candles_5m[0]["time"]) / 86400.0 if candles_5m else 0.0
    result = SymbolResult(symbol=symbol, bars=len(candles_5m), span_days=span_days)
    if not candles_5m:
        return result
    c15m = resample(candles_5m, 900)
    info = load_symbol_info(symbol, candles_dir)
    cval = contract_value(info)
    sl_mult, tp_mult = cfg.trading.sl_atr_mult, cfg.trading.tp_atr_mult
    band = float(getattr(cfg.trading, "scalp_pullback_band", 1.5))
    min_conf = cfg.trading.min_confidence
    equity, open_trade, last_bucket = equity_start, None, -1
    bucket_index: dict[int, int] = {}
    cursor = -1
    for bar in candles_5m:
        bucket = int(bar["time"]) // 900 * 900
        if bucket in bucket_index:
            continue
        while cursor + 1 < len(c15m) and int(c15m[cursor + 1]["time"]) <= bucket:
            cursor += 1
        bucket_index[bucket] = cursor

    for i in range(20, len(candles_5m) - 1):
        bar = candles_5m[i]
        bucket = int(bar["time"]) // 900 * 900
        if open_trade is not None:
            nxt = candles_5m[i + 1]
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
                fees = commission * open_trade.lots + spread * cval * open_trade.lots
                open_trade.fees_usd = fees
                open_trade.pnl_usd = gross - fees
                equity += open_trade.pnl_usd
                result.trades.append(open_trade)
                open_trade = None
                continue
            if breakeven:
                if is_buy and hi - open_trade.entry >= BREAKEVEN_ATR * open_trade.atr \
                        and open_trade.sl < open_trade.entry:
                    open_trade.sl = open_trade.entry + BREAKEVEN_OFFSET_ATR * open_trade.atr
                elif not is_buy and open_trade.entry - lo >= BREAKEVEN_ATR * open_trade.atr \
                        and open_trade.sl > open_trade.entry:
                    open_trade.sl = open_trade.entry - BREAKEVEN_OFFSET_ATR * open_trade.atr
            continue
        if bucket == last_bucket:
            continue
        end = bucket_index.get(bucket, -1)
        if end < 49:
            continue
        c15_window = c15m[end - 59 : end + 1]
        c5_window = candles_5m[i - 19 : i + 1]
        price = float(bar["close"])
        action, _c, _r, atr = compute_scalp_5m_signal(c15_window, c5_window, price, band)
        if action == "WAIT" or atr <= 0:
            continue
        bullish = action == "BUY"
        atr15 = compute_atr(c15_window, 14)
        if atr15 <= 0:
            continue
        atr_series = []
        for stop in range(len(c15_window) - 1, max(len(c15_window) - 31, 14), -1):
            value = compute_atr(c15_window[: stop + 1], 14)
            if value > 0:
                atr_series.append(value)
        trend_closes = [float(b["close"]) for b in c15_window]
        confidence = compute_setup_confidence(
            compute_ema(trend_closes, 21), compute_ema(trend_closes, 50),
            price, atr15, atr_series, bullish)
        if confidence < min_conf:
            continue
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
    return result


def main():
    ap = argparse.ArgumentParser(description="Historical deterministic strategy backtest.")
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--candles-dir", type=Path, default=None)
    ap.add_argument("--candles-dir-m5", type=Path, default=None)
    ap.add_argument("--write-candles", type=Path, default=None)
    ap.add_argument("--strategy", choices=("pullback", "breakout", "scalp_5m"), default="pullback")
    ap.add_argument("--no-breakeven", action="store_true")
    ap.add_argument("--commission", type=float, default=DEFAULT_COMMISSION_PER_LOT)
    ap.add_argument("--risk-pct", type=float, default=.5)
    ap.add_argument("--equity", type=float, default=10000.)
    ap.add_argument("--walk-forward", action="store_true")
    args = ap.parse_args()
    if args.strategy == "scalp_5m" and args.candles_dir_m5 is None:
        print("scalp_5m requires --candles-dir-m5", file=sys.stderr)
        return 2

    cfg = Config.from_env()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    required_days = args.months * 30.
    results, insufficient, loaded = [], [], []

    def run(sym, bars, breakeven):
        spread = DEFAULT_SPREAD_PRICE.get(sym, 0.)
        if args.strategy == "scalp_5m":
            return run_symbol_scalp(sym, bars, cfg, spread, args.commission, args.risk_pct,
                                    args.equity, breakeven, directory)
        return run_symbol(sym, bars, cfg, spread, args.commission, args.risk_pct, args.equity,
                          args.strategy, breakeven, args.candles_dir)

    for symbol in symbols:
        directory = args.candles_dir_m5 if args.strategy == "scalp_5m" else args.candles_dir
        timeframe = "5m" if args.strategy == "scalp_5m" else "15m"
        try:
            candles = load_candles(symbol, args.months, directory, timeframe)
        except Exception as exc:
            print(f"{symbol}: data error: {exc}", file=sys.stderr)
            insufficient.append(f"{symbol}: {exc}")
            continue
        span = (candles[-1]["time"] - candles[0]["time"]) / 86400. if candles else 0.
        if span < required_days:
            insufficient.append(f"{symbol}: {len(candles)} bars spanning {span:.1f} days "
                                f"< required {required_days:.0f} days")
            continue
        if args.write_candles is not None:
            args.write_candles.mkdir(parents=True, exist_ok=True)
            (args.write_candles / f"{symbol}_{timeframe}.json").write_text(json.dumps(candles))
        try:
            metadata_dir = Path("data/candles")
            load_symbol_info(symbol, metadata_dir)
        except Exception as exc:
            print(f"{symbol}: broker metadata error: {exc}", file=sys.stderr)
            insufficient.append(f"{symbol}: broker metadata: {exc}")
            continue
        results.append(run(symbol, candles, not args.no_breakeven))
        loaded.append((symbol, candles))

    oos_ok = None
    if args.walk_forward and not insufficient:
        print("\nWALK-FORWARD (first half in-sample, second half out-of-sample):")
        oos_trades = []
        for sym, full in loaded:
            mid = len(full) // 2
            ins = run(sym, full[:mid], not args.no_breakeven)
            outs = run(sym, full[max(0, mid - 60):], not args.no_breakeven)
            outs.trades = [t for t in outs.trades if t.entry_time >= full[mid]["time"]]
            oos_trades.extend(outs.trades)
            a, b = summarize([ins]), summarize([outs])
            print(f"  {sym:7s} IS: tr={a['trades']:3d} exp={a['expectancy_usd']:7.2f} "
                  f"PF={a['profit_factor']:.3f} wr={a['win_rate']:4.1f}% | "
                  f"OOS: tr={b['trades']:3d} exp={b['expectancy_usd']:7.2f} "
                  f"PF={b['profit_factor']:.3f} wr={b['win_rate']:4.1f}%")
        oos = summarize([SymbolResult("OOS", 0, 0., trades=oos_trades)])
        print(f"OOS total: trades={oos['trades']} expectancy_usd={oos['expectancy_usd']:.2f} "
              f"profit_factor={oos['profit_factor']:.3f} win_rate={oos['win_rate']:.1f}%")
        oos_ok = oos["expectancy_usd"] > 0 and oos["profit_factor"] > 1.1 and oos["trades"] >= 30

    for res in results:
        st = summarize([res])
        print(f"{res.symbol:7s} bars={res.bars:6d} span_days={res.span_days:6.1f} "
              f"signals={res.signals:4d} trades={len(res.trades):4d} "
              f"exp_usd={st['expectancy_usd']:8.2f} pf={st['profit_factor']:.3f} "
              f"wr={st['win_rate']:5.1f}%")
    if insufficient:
        print("\nDATA INSUFFICIENT — gate result NOT recorded:")
        for line in insufficient:
            print(f"  - {line}")
        print("Supply >= 6 months of candles via --candles-dir / --candles-dir-m5.")
        return 2
    stats = summarize(results)
    print(f"\nstrategy={args.strategy} breakeven={not args.no_breakeven} "
          f"risk_pct={args.risk_pct} equity_start={args.equity:.0f}")
    print(f"trades={stats['trades']} expectancy_usd={stats['expectancy_usd']:.2f} "
          f"profit_factor={stats['profit_factor']:.3f} win_rate={stats['win_rate']:.1f}% "
          f"max_dd_usd={stats['max_drawdown_usd']:.2f} fees_usd={stats['fees_usd']:.2f}")
    if stats["r_multiples"]:
        print(f"mean_R={statistics.fmean(stats['r_multiples']):.3f} "
              f"median_R={statistics.median(stats['r_multiples']):.3f}")
    passed = stats["expectancy_usd"] > 0 and stats["profit_factor"] > 1.1 and stats["trades"] >= 30
    if args.walk_forward:
        passed = passed and bool(oos_ok)
    label = "PASS" if passed else "FAIL"
    extra = ", plus out-of-sample" if args.walk_forward else ""
    print(f"\nB9 BACKTEST GATE: {label} (expectancy>0, profit_factor>1.1, "
          f"at least 30 trades{extra})")
    return 0 if passed else 1

if __name__=="__main__":raise SystemExit(main())
