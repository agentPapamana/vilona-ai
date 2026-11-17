"""Micro-technical indicator calculations and M5 scalp signal generation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ScalpSignal:
    action: str  # "BUY", "SELL", "WAIT"
    confidence: float
    reason: str
    atr_5m: float
    spread: float
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    rsi: float = 50.0


def compute_atr(rates: List[Dict[str, Any]], period: int = 14) -> float:
    """Calculate Average True Range from candle dictionary list."""
    if not rates or len(rates) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(rates)):
        h = float(rates[i].get("high", 0.0))
        l = float(rates[i].get("low", 0.0))
        prev_c = float(rates[i - 1].get("close", 0.0))
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return sum(trs[-period:]) / float(period)


def compute_ema(series: List[float], period: int) -> float:
    """Exponential Moving Average."""
    if not series:
        return 0.0
    if len(series) < period:
        return sum(series) / float(len(series))
    k = 2.0 / (period + 1.0)
    ema_val = series[0]
    for v in series[1:]:
        ema_val = (v * k) + (ema_val * (1.0 - k))
    return ema_val


def compute_rsi(series: List[float], period: int = 14) -> float:
    """Relative Strength Index over a series of closing prices."""
    if not series or len(series) < period + 1:
        return 50.0
    deltas = [series[i] - series[i - 1] for i in range(1, len(series))]
    gains = [d if d > 0.0 else 0.0 for d in deltas[-period:]]
    losses = [-d if d < 0.0 else 0.0 for d in deltas[-period:]]
    avg_gain = sum(gains) / float(period)
    avg_loss = sum(losses) / float(period)
    if avg_loss <= 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_trend_bias(
    candles: List[Dict[str, Any]], fast_period: int = 21, slow_period: int = 50
) -> str:
    """Determine trend bias ('BULLISH', 'BEARISH', or 'FLAT') from candle series."""
    if not candles or len(candles) < slow_period:
        return "FLAT"
    closes = [float(c.get("close", 0.0)) for c in candles]
    ema_fast = compute_ema(closes, fast_period)
    ema_slow = compute_ema(closes, slow_period)
    cur_close = closes[-1]

    if ema_fast > ema_slow and cur_close >= ema_fast:
        return "BULLISH"
    elif ema_fast < ema_slow and cur_close <= ema_fast:
        return "BEARISH"
    return "FLAT"


def compute_m5_scalp_signal(
    c5m: List[Dict[str, Any]],
    c15m: List[Dict[str, Any]],
    c1h: Optional[List[Dict[str, Any]]],
    bid: float,
    ask: float,
    max_spread_points: float = 1.5,
    min_confidence: float = 0.60,
    ema_fast_p: int = 9,
    ema_slow_p: int = 21,
    trend_filter_p: int = 50,
    band_multiplier: float = 1.2,
    rsi_p: int = 14,
    rsi_ob: float = 70.0,
    rsi_os: float = 30.0,
) -> ScalpSignal:
    """Deterministic M5 scalp signal evaluation. 100% native code, zero AI dependency."""
    spread = max(0.0, ask - bid)

    # 1. Spread Check: Strict absolute spread guard
    if spread > max_spread_points:
        return ScalpSignal(
            action="WAIT",
            confidence=0.0,
            reason=f"spread {spread:.3f} exceeds max {max_spread_points:.3f}",
            atr_5m=0.0,
            spread=spread,
        )

    # 2. Candle Sufficiency Check
    if len(c5m) < 25 or len(c15m) < 30:
        return ScalpSignal(
            action="WAIT",
            confidence=0.0,
            reason="insufficient candles for M5 scalp",
            atr_5m=0.0,
            spread=spread,
        )

    atr_5m = compute_atr(c5m, 14)
    if atr_5m <= 0.0:
        return ScalpSignal(
            action="WAIT",
            confidence=0.0,
            reason="invalid M5 ATR",
            atr_5m=0.0,
            spread=spread,
        )

    closes_5m = [float(c.get("close", 0.0)) for c in c5m]
    ema_fast = compute_ema(closes_5m, ema_fast_p)
    ema_slow = compute_ema(closes_5m, ema_slow_p)
    rsi_5m = compute_rsi(closes_5m, rsi_p)
    # 3. Market Volume Confirmation (Detect market activity & liquidity)
    volumes = [float(c.get("tick_volume", 0) or c.get("volume", 0) or 0.0) for c in c5m]
    # Evaluate completed bar volume (volumes[-2]) so an unformed newly opened bar does not false-veto
    last_completed_vol = volumes[-2] if len(volumes) >= 2 else (volumes[-1] if volumes else 0.0)
    avg_vol = (sum(volumes[-11:-1]) / 10.0) if len(volumes) >= 11 else last_completed_vol
    vol_active = (last_completed_vol >= 0.25 * avg_vol) if avg_vol > 0 else True

    if not vol_active:
        return ScalpSignal(
            action="WAIT",
            confidence=0.0,
            reason=f"market volume dead (vol={recent_vol:.0f} < avg={avg_vol:.0f})",
            atr_5m=atr_5m,
            spread=spread,
        )

    # 4. Higher Timeframe Alignment
    m15_bias = compute_trend_bias(c15m, 21, trend_filter_p)
    h1_bias = compute_trend_bias(c1h, 21, trend_filter_p) if c1h and len(c1h) >= 30 else "FLAT"

    price = bid
    band = band_multiplier * atr_5m

    # 4. BUY Setup Evaluation
    # Trend alignment: H1/M15 must not be explicitly BEARISH
    if m15_bias != "BEARISH" and h1_bias != "BEARISH":
        if ema_fast > ema_slow and rsi_5m <= 75.0:
            dist = abs(price - ema_fast)
            if dist <= band:
                # Score setup confidence
                trend_strength = 0.85 if (m15_bias == "BULLISH" and h1_bias == "BULLISH") else 0.70
                pullback_qual = max(0.25, 1.0 - (dist / band))
                rsi_health = 1.0 if 45.0 <= rsi_5m <= 70.0 else 0.8
                confidence = round(0.4 * trend_strength + 0.4 * pullback_qual + 0.2 * rsi_health, 2)

                if confidence >= min_confidence:
                    return ScalpSignal(
                        action="BUY",
                        confidence=confidence,
                        reason=f"M5 scalp BUY (RSI={rsi_5m:.1f}, M15={m15_bias}, H1={h1_bias})",
                        atr_5m=atr_5m,
                        spread=spread,
                        ema_fast=ema_fast,
                        ema_slow=ema_slow,
                        rsi=rsi_5m,
                    )

    # 5. SELL Setup Evaluation
    # Trend alignment: H1/M15 must not be explicitly BULLISH
    if m15_bias != "BULLISH" and h1_bias != "BULLISH":
        if ema_fast < ema_slow and rsi_5m >= 5.0:
            dist = abs(ema_fast - price)
            if dist <= band:
                trend_strength = 0.85 if (m15_bias == "BEARISH" and h1_bias == "BEARISH") else 0.70
                pullback_qual = max(0.25, 1.0 - (dist / band))
                rsi_health = 1.0 if 15.0 <= rsi_5m <= 55.0 else 0.8
                confidence = round(0.4 * trend_strength + 0.4 * pullback_qual + 0.2 * rsi_health, 2)

                if confidence >= min_confidence:
                    return ScalpSignal(
                        action="SELL",
                        confidence=confidence,
                        reason=f"M5 scalp SELL (RSI={rsi_5m:.1f}, M15={m15_bias}, H1={h1_bias})",
                        atr_5m=atr_5m,
                        spread=spread,
                        ema_fast=ema_fast,
                        ema_slow=ema_slow,
                        rsi=rsi_5m,
                    )

    return ScalpSignal(
        action="WAIT",
        confidence=0.0,
        reason="no qualifying M5 scalp setup",
        atr_5m=atr_5m,
        spread=spread,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=rsi_5m,
    )
