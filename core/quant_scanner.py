"""
Vilona AI — Autonomous Quantitative Market Scanner & Trade Manager
Continuously scans symbols, evaluates multi-timeframe indicators, runs agent deliberation,
and manages trailing/break-even on open positions.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Any

from config import get_config, TradingConfig
from agents import Signal, MarketSnapshot
from agents.risk_manager import evaluate as risk_evaluate
from core.state_db import StateDB
from core.judge_circuit_breaker import JudgeCircuitBreaker
from core.risk_lock import (
    FileKillSwitch,
    consecutive_loss_count,
    daily_pl_from_bridge_day,
    entry_lock_from_bridge,
    evaluate_consecutive_losses,
    evaluate_entry_lock,
    evaluate_total_drawdown,
)
from core.market_guard import evaluate_tick_freshness, evaluate_weekend_guard, is_weekend_flatten_window
from core.bridge_heartbeat import evaluate_bridge_heartbeat

logger = logging.getLogger("vilona-ai.scanner")

# Broker-deal lookback used to match positions that have gone away. Wide enough
# that a weekend or a scan outage does not lose the closing deal.
CLOSED_TRADE_LOOKBACK_SECONDS = 7 * 24 * 3600


def _trade_result(result) -> tuple:
    """Normalise bridge modify/close return shapes to (ok, message)."""
    if isinstance(result, tuple):
        ok = bool(result[0]) if result else False
        msg = str(result[1]) if len(result) > 1 else ""
        return ok, msg
    if result is None:
        return False, "no result returned"
    ok = getattr(result, "success", None)
    if ok is None:
        return True, ""
    return bool(ok), str(getattr(result, "message", "") or "")


def _close_enough(actual, requested, tol: float = 1e-9) -> bool:
    """Compare a readback SL/TP against the request; 0/None means 'not set'."""
    req = float(requested or 0.0)
    if req == 0.0:
        return float(actual or 0.0) == 0.0
    try:
        return abs(float(actual) - req) <= max(tol, abs(req) * 1e-6)
    except (TypeError, ValueError):
        return False

def _symbol_digits(symbol_info: Any) -> Optional[int]:
    """Broker price digits for SL/TP rounding.

    Returns None when the broker did not report them, so the order keeps full
    precision instead of being rounded to a guessed (and possibly coarser)
    value. Rounding EURUSD to 2 digits moved a 7.5 pip stop to 50 pips.
    """
    digits = getattr(symbol_info, "digits", None)
    try:
        digits = int(digits)
    except (TypeError, ValueError):
        return None
    return digits if 0 <= digits <= 8 else None


# Profit-lock ladder: (progress toward TP, share of the TP distance locked by SL).
# A 0.01 lot cannot be partially closed, so the gain is banked by ratcheting the
# stop instead. Locks always trail price by at least a quarter of the TP
# distance, so normal pullbacks do not stop the trade out early.
PROFIT_LOCK_LADDER = ((0.75, 0.50), (0.50, 0.25))


def profit_lock_sl(*, is_buy: bool, open_px: float, cur_px: float, tp: float,
                   cur_sl: float, atr: float, digits: int) -> Optional[float]:
    """Most protective stop the ladder allows now, or None when no tightening applies.

    Break-even (entry +/- 0.1 ATR once the trade is 1 ATR in profit) stays as the
    first step; the ladder adds locks at 50% and 75% of the way to TP. The result
    only ever tightens the current stop and stays on the losing side of price.
    """
    direction = 1.0 if is_buy else -1.0
    gain = (cur_px - open_px) * direction
    if gain <= 0:
        return None
    candidates = []
    if atr > 0 and gain >= atr:
        candidates.append(open_px + direction * 0.1 * atr)
    tp_dist = (tp - open_px) * direction if tp else 0.0
    if tp_dist > 0:
        progress = gain / tp_dist
        for threshold, lock in PROFIT_LOCK_LADDER:
            if progress >= threshold:
                candidates.append(open_px + direction * lock * tp_dist)
                break
    if not candidates:
        return None
    target = round(max(candidates) if is_buy else min(candidates), digits)
    tick = 10.0 ** -digits
    if (cur_px - target) * direction < tick:
        return None  # would sit at or beyond current price
    if cur_sl > 0 and (target - cur_sl) * direction < tick:
        return None  # not tighter than the stop already held
    return target

def compute_atr(rates: List[Dict[str, Any]], period: int = 14) -> float:
    if len(rates) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(rates)):
        h = float(rates[i].get("high", 0))
        l = float(rates[i].get("low", 0))
        prev_c = float(rates[i - 1].get("close", 0))
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return sum(trs[-period:]) / float(period)


def compute_ema(series: List[float], period: int) -> float:
    if not series:
        return 0.0
    k = 2.0 / (period + 1.0)
    ema_val = series[0]
    for v in series[1:]:
        ema_val = (v * k) + (ema_val * (1.0 - k))
    return ema_val


def compute_rsi(series: List[float], period: int = 14) -> float:
    """Relative Strength Index over a sequence of closing prices."""
    if len(series) < period + 1:
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


def trend_tp_target(*, is_buy: bool, open_px: float, cur_px: float, current_tp: float,
                    atr: float, closes: List[float]) -> Optional[float]:
    """Extend TP during a confirmed short-term trend; never retract a target."""
    if atr <= 0 or len(closes) < 22:
        return None
    ema_fast = compute_ema(closes[-21:], 9)
    ema_slow = compute_ema(closes[-21:], 21)
    aligned = ema_fast > ema_slow if is_buy else ema_fast < ema_slow
    directional_move = (cur_px - open_px) if is_buy else (open_px - cur_px)
    tp_dist = ((current_tp - open_px) if is_buy else (open_px - current_tp)) if current_tp > 0 else 0.0
    # Avoid moving a distant target early: extend only once price is near it.
    if not aligned or directional_move < 0.5 * atr or tp_dist <= 0 or directional_move < 0.7 * tp_dist:
        return None
    candidate = cur_px + (2.0 * atr if is_buy else -2.0 * atr)
    cap = open_px + (6.0 * atr if is_buy else -6.0 * atr)
    candidate = min(candidate, cap) if is_buy else max(candidate, cap)
    improves = candidate > current_tp if is_buy else candidate < current_tp
    return candidate if improves else None


def _error_type_from(detail: str, original_exc: Optional[BaseException] = None) -> str:
    """Extract a stable error class name from an llm_gate error detail string.

    The gate formats failures as "LLM judge call failed: <message>" or
    "LLM judge returned empty response"; the message may or may not contain the
    exception class. The audit column should hold a short type, never prose.
    """
    if original_exc is not None:
        return type(original_exc).__name__
    text = detail or ""
    for marker in ("TimeoutError", "Timeout", "timed out", "ConnectionError",
                   "HTTPStatusError", "JSONDecodeError", "empty response"):
        if marker.lower() in text.lower():
            if marker in ("Timeout", "timed out"):
                return "TimeoutError"
            if marker == "empty response":
                return "EmptyResponseError"
            return marker
    return "LLMError"


CONFIDENCE_FLOOR = 0.40
CONFIDENCE_CEIL = 0.95


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_setup_confidence(
    ema21: float,
    ema50: float,
    price: float,
    atr: float,
    atr_series: List[float],
    bullish: bool,
) -> float:
    """Score a trend-pullback setup on measurable quality, not a fixed number.

    Four independent components, each normalized to 0..1, then blended so that
    a marginal setup lands below the risk gate threshold and a clean one lands
    well above it:

    1. Trend separation  — EMA21/EMA50 gap in ATR units. A flat pair (crossover
       noise) scores near 0; a clean directional spread scores near 1.
    2. Pullback quality  — how close price sits to EMA21 relative to the allowed
       1.2 ATR band. Entering at the band edge is a chase; entering close to the
       mean is a real pullback.
    3. Volatility regime — ATR against its own recent average. Dead or spiking
       volatility both reduce score, a stable expanding regime scores high.
    4. Slope agreement   — whether EMA21 is on the correct side of EMA50 with a
       material distance, confirming the trend is directional not oscillating.

    Returns a confidence in [CONFIDENCE_FLOOR, CONFIDENCE_CEIL].
    """
    if atr <= 0 or ema21 <= 0 or ema50 <= 0 or price <= 0:
        return CONFIDENCE_FLOOR

    # 1. Trend separation, saturating at 2.0 ATR of EMA gap.
    separation_atr = abs(ema21 - ema50) / atr
    trend_score = _clamp(separation_atr / 2.0, 0.0, 1.0)

    # 2. Pullback quality: 0 at the 1.2 ATR band edge, 1 at the EMA itself.
    distance = (price - ema21) if bullish else (ema21 - price)
    band = 1.2 * atr
    if distance < 0:
        # Price on the wrong side of EMA21 within the band — shallow fade.
        pullback_score = 0.25
    else:
        pullback_score = _clamp(1.0 - (distance / band), 0.0, 1.0)

    # 3. Volatility regime: ATR vs its own trailing mean.
    vol_score = 0.5
    if atr_series:
        mean_atr = sum(atr_series) / float(len(atr_series))
        if mean_atr > 0:
            ratio = atr / mean_atr
            # Peak at ratio 1.0-1.3, taper on both sides.
            if ratio <= 1.0:
                vol_score = _clamp(ratio, 0.0, 1.0)
            else:
                vol_score = _clamp(1.0 - ((ratio - 1.3) / 1.7), 0.0, 1.0)

    # 4. Slope/direction agreement via the EMA gap already computed.
    slope_score = trend_score

    blended = (
        0.40 * trend_score
        + 0.30 * pullback_score
        + 0.15 * vol_score
        + 0.15 * slope_score
    )
    return round(_clamp(CONFIDENCE_FLOOR + blended * (CONFIDENCE_CEIL - CONFIDENCE_FLOOR),
                        CONFIDENCE_FLOOR, CONFIDENCE_CEIL), 3)


def compute_scalp_5m_signal(c15m: List[Dict[str, Any]], c5m: List[Dict[str, Any]],
                           price: float, band_mult: float,
                           c1h: Optional[List[Dict[str, Any]]] = None) -> tuple[str, float, str, float]:
    """Return a conservative 15m-trend/5m-pullback signal and its ATR.

    Requires:
    1. Multi-timeframe trend alignment: when 1h candles are provided, 1h EMA21/50
       must agree with the 15m trend (never buy into a 1-hour bear trend).
    2. Minimum 15m trend strength (0.25 ATR) to reject flat sideways ranges.
    3. 5m RSI between 30 and 70 to avoid buying tops or selling flushes.
    4. Price within band_mult * ATR from the 5m trigger EMA.
    """
    if len(c15m) < 50 or len(c5m) < 20 or price <= 0:
        return "WAIT", 0.0, "insufficient scalp candles", 0.0
    trend_closes = [float(row.get("close", 0)) for row in c15m]
    ema21 = compute_ema(trend_closes, 21)
    ema50 = compute_ema(trend_closes, 50)
    atr = compute_atr(c5m, 14)
    if atr <= 0 or ema21 <= 0 or ema50 <= 0:
        return "WAIT", 0.0, "invalid scalp indicators", atr
    entry = float(price)
    c5m_closes = [float(row.get("close", 0)) for row in c5m]
    trigger_ema = compute_ema(c5m_closes, 21)
    distance = abs(entry - trigger_ema)
    trend_strength = abs(ema21 - ema50) / atr
    if trend_strength < 0.25:
        return "WAIT", 0.0, f"15m trend too weak ({trend_strength:.2f} ATR)", atr

    # 1H Trend Alignment (Multi-timeframe gate):
    if c1h and len(c1h) >= 50:
        closes_1h = [float(row.get("close", 0)) for row in c1h]
        ema21_1h = compute_ema(closes_1h, 21)
        ema50_1h = compute_ema(closes_1h, 50)
        if ema21 > ema50 and ema21_1h < ema50_1h:
            return "WAIT", 0.0, f"1h trend bearish ({ema21_1h:.4f} < {ema50_1h:.4f}) opposes 15m buy", atr
        if ema21 < ema50 and ema21_1h > ema50_1h:
            return "WAIT", 0.0, f"1h trend bullish ({ema21_1h:.4f} > {ema50_1h:.4f}) opposes 15m sell", atr

    # 5M RSI filter:
    rsi = compute_rsi(c5m_closes, 14)
    if ema21 > ema50 and rsi > 70.0:
        return "WAIT", 0.0, f"5m RSI overbought ({rsi:.1f} > 70)", atr
    if ema21 < ema50 and rsi < 30.0:
        return "WAIT", 0.0, f"5m RSI oversold ({rsi:.1f} < 30)", atr

    if ema21 > ema50 and entry >= (trigger_ema - 0.5 * atr) and distance <= band_mult * atr:
        return "BUY", 0.70, f"15m bullish trend, 5m pullback {distance / atr:.2f} ATR (RSI {rsi:.1f})", atr
    if ema21 < ema50 and entry <= (trigger_ema + 0.5 * atr) and distance <= band_mult * atr:
        return "SELL", 0.70, f"15m bearish trend, 5m pullback {distance / atr:.2f} ATR (RSI {rsi:.1f})", atr
    side = "bull" if ema21 > ema50 else "bear"
    where = "above" if entry > trigger_ema else "below"
    return (
        "WAIT", 0.0,
        f"no 15m trend / 5m pullback setup (15m {side} {trend_strength:.2f} ATR, "
        f"price {where} 5m EMA21 by {distance / atr:.2f} ATR, band {band_mult:.2f})",
        atr,
    )


def compute_breakout_signal(candles_15m: List[Dict[str, Any]], candles_1h: List[Dict[str, Any]], price: float, lookback: int = 20) -> tuple[str, float, str, float]:
    """Return a 15m range-break signal aligned with the 1h EMA trend."""
    if len(candles_15m) < max(lookback + 1, 20) or len(candles_1h) < 50 or price <= 0:
        return "WAIT", 0.0, "insufficient breakout candles", 0.0
    atr = compute_atr(candles_15m, 14)
    closes_1h = [float(row.get("close", 0)) for row in candles_1h]
    ema21 = compute_ema(closes_1h, 21)
    ema50 = compute_ema(closes_1h, 50)
    if atr <= 0 or ema21 <= 0 or ema50 <= 0:
        return "WAIT", 0.0, "invalid breakout indicators", atr
    previous = candles_15m[-lookback - 1:-1]
    high = max(float(row.get("high", 0)) for row in previous)
    low = min(float(row.get("low", 0)) for row in previous)
    if price > high and ema21 > ema50:
        strength = min(0.95, 0.55 + max(0.0, price - high) / atr * 0.2)
        return "BUY", round(strength, 3), f"15m high breakout {price:.5f}>{high:.5f}, 1h bullish EMA", atr
    if price < low and ema21 < ema50:
        strength = min(0.95, 0.55 + max(0.0, low - price) / atr * 0.2)
        return "SELL", round(strength, 3), f"15m low breakout {price:.5f}<{low:.5f}, 1h bearish EMA", atr
    return "WAIT", 0.0, "no aligned 15m range breakout", atr


class QuantScanner:
    def __init__(self, bridge, cfg=None, tg_controller=None, dashboard=None):
        self.bridge = bridge
        self.cfg = cfg or get_config()
        self.tg_controller = tg_controller
        self.dashboard = dashboard
        self.db = StateDB()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.poll_interval = 30.0  # seconds between scan cycles
        # symbol -> epoch seconds of the last accepted entry, for cooldown gating
        self._last_entry_at: Dict[str, float] = {}
        # symbol -> deterministic setup fingerprint. A cooldown alone is not
        # enough: after restart or a short cooldown, the same unchanged setup
        # must not generate duplicate paper/live intents.
        self._last_setup_key: Dict[str, str] = {}
        self._kill_switch = FileKillSwitch(str(getattr(self.cfg, "kill_switch_path", "/tmp/vilona-STOP_ENTRIES")))
        breaker_cfg = getattr(self.cfg, "judge_breaker", None)
        self.judge_breaker = JudgeCircuitBreaker(
            failure_threshold=int(getattr(breaker_cfg, "failure_threshold", 3)),
            cooldown_seconds=float(getattr(breaker_cfg, "cooldown_seconds", 120.0)),
        )
        # ticket -> snapshot of the last observed position, for closed-trade rows.
        self._tracked_positions: Dict[int, Dict[str, Any]] = {}
        # Everything we have ever opened this process; a ticket leaving the book
        # is how a close is detected.
        self._known_tickets: set = set()
        # Bridge heartbeat is tracked independently for each routed venue.
        self._last_heartbeat_tick_s: Dict[str, Optional[float]] = {}
        self._heartbeat_down: Dict[str, bool] = {}
        # ticket -> epoch seconds of the last LLM position review, for rate limiting
        self._last_position_review: Dict[int, float] = {}

    def _is_forex_venue(self) -> bool:
        """True when the connected backend trades a weekday-only market.

        MT5 is a forex/metals venue with a weekly close, so the weekend entry
        and flatten windows apply. CCXT venues (Binance, Bybit, ...) trade
        crypto around the clock and must never be locked out on a weekend.
        """
        if "mt5" in type(self.bridge).__name__.lower():
            return True
        exchange = getattr(self.cfg, "exchange", None)
        return str(getattr(exchange, "name", "")).lower() == "mt5"

    def _is_forex_symbol(self, symbol: str) -> bool:
        bridge_for = getattr(self.bridge, "bridge_for", None)
        if bridge_for is not None:
            try:
                return "mt5" in type(bridge_for(symbol)).__name__.lower()
            except (KeyError, TypeError):
                return False
        return self._is_forex_venue()

    def _record_position_decision(self, **decision):
        """Persist a position-manager decision; DB failure must not stop trading."""
        if self.db is None or not hasattr(self.db, "save_position_decision"):
            return
        try:
            self.db.save_position_decision(decision)
        except Exception:
            logger.exception("Failed to persist position decision for ticket=%s", decision.get("ticket"))

    def _market_deals(self) -> Optional[List[Dict[str, Any]]]:
        """All closed deals in the lookback window, or None when the broker cannot say."""
        if not hasattr(self.bridge, "get_history_deals"):
            return None
        since = int(time.time()) - CLOSED_TRADE_LOOKBACK_SECONDS
        try:
            deals = self.bridge.get_history_deals(since)
        except Exception:
            logger.exception("Closed-deal history lookup failed")
            return None
        if deals is None:
            return None
        return [d for d in deals if isinstance(d, dict)]

    def persist_closed_trades(self):
        """Record trades whose positions vanished from the book since the last scan.

        A ticket we opened and later no longer see has closed. The broker deal
        history is the only authoritative profit source: if it is unavailable or
        carries no matching deal, nothing is written and a warning is emitted
        rather than a fabricated profit row.
        """
        if self.db is None or not hasattr(self.db, "save_trade"):
            return
        open_tickets = self._open_tickets()
        missing = (self._known_tickets - open_tickets) | (
            set(self._tracked_positions) - open_tickets
        )
        if not missing:
            return
        deals = self._market_deals()
        if deals is None:
            self._feed(
                f"CLOSED TRADE SKIPPED: {len(missing)} ticket(s) closed but broker "
                f"history is unavailable; no profit recorded"
            )
            logger.warning("Broker history unavailable; not persisting %d closed ticket(s)", len(missing))
            return
        for ticket in sorted(missing):
            record = self._tracked_positions.get(ticket) or {}
            try:
                if hasattr(self.db, "has_trade") and self.db.has_trade(ticket):
                    continue
                deal = self._matching_close_deal(deals, ticket, record.get("symbol"))
                if deal is None:
                    self._feed(
                        f"CLOSED TRADE SKIPPED ticket={ticket}: no matching closing deal "
                        f"in broker history; refusing to fabricate profit"
                    )
                    logger.warning("No closing deal for ticket %s; no trade row written", ticket)
                    continue
                from core.order_safety import MAGIC_BASE
                if "magic" in deal:
                    try:
                        magic = int(deal["magic"])
                    except (TypeError, ValueError):
                        continue
                    if not (MAGIC_BASE <= magic < MAGIC_BASE + 1000):
                        continue
                if any(deal.get(field) is None for field in ("profit", "commission", "swap", "time")):
                    continue
                profit = float(deal.get("profit"))
                commission = float(deal.get("commission"))
                swap = float(deal.get("swap"))
                exit_price = deal.get("price")
                saved = self.db.save_trade({
                    "ticket": ticket,
                    "symbol": deal.get("symbol") or record.get("symbol"),
                    "side": record.get("side"),
                    "quantity": record.get("quantity"),
                    "entry_price": record.get("entry_price"),
                    "exit_price": float(exit_price) if exit_price is not None else None,
                    "sl": record.get("sl"),
                    "tp": record.get("tp"),
                    "profit": profit,
                    "commission": commission,
                    "swap": swap,
                    "strategy": record.get("strategy", "vilona-auto"),
                    "reason": record.get("reason", "closed: broker deal history"),
                    "opened_at": record.get("opened_at") or time.time(),
                    "closed_at": float(deal.get("time") or time.time()),
                    "status": "closed",
                })
                if saved:
                    self._feed(f"CLOSED TRADE ticket={ticket} profit={profit}")
            except Exception:
                logger.exception("Failed to persist closed trade for ticket=%s", ticket)

    def _matching_close_deal(self, deals, ticket, symbol):
        """Pick the closing deal for a ticket, verifying symbol when both sides know it."""
        wanted = int(ticket)
        for deal in deals:
            if "position_id" in deal and deal.get("position_id") is not None:
                deal_ticket = deal.get("position_id")
            else:
                deal_ticket = deal.get("position", deal.get("ticket"))
            if deal_ticket is None:
                continue
            try:
                if int(deal_ticket) != wanted:
                    continue
            except (TypeError, ValueError):
                continue
            deal_symbol = deal.get("symbol")
            if symbol and deal_symbol and str(deal_symbol) != str(symbol):
                continue
            return deal
        return None

    def _open_tickets(self) -> set:
        """Tickets currently open at the broker; empty when the book is unreadable."""
        try:
            positions = self.bridge.get_positions() or []
        except Exception:
            logger.exception("Open-position read failed")
            return set()
        out = set()
        for pos in positions:
            try:
                out.add(int(pos.ticket))
            except (TypeError, ValueError, AttributeError):
                continue
        return out

    def _snapshot_open_positions(self):
        """Capture Vilona-owned positions after management for later close detection."""
        try:
            positions = self.bridge.get_positions() or []
        except Exception:
            return
        from core.order_safety import MAGIC_BASE
        for pos in positions:
            magic = int(getattr(pos, "magic", 0) or 0)
            if not (MAGIC_BASE <= magic < MAGIC_BASE + 1000):
                continue
            try:
                ticket = int(pos.ticket)
            except (TypeError, ValueError, AttributeError):
                continue
            self._known_tickets.add(ticket)
            self._tracked_positions[ticket] = {
                "symbol": pos.symbol,
                "side": "buy" if pos.type == 0 or str(pos.type).lower() == "buy" else "sell",
                "quantity": getattr(pos, "volume", getattr(pos, "quantity", 0.0)),
                "entry_price": getattr(pos, "price_open", None),
                "sl": getattr(pos, "sl", None),
                "tp": getattr(pos, "tp", None),
                "opened_at": getattr(pos, "time", None),
            }

    def _publish_market_data(self, symbol: str, tick: Any, candles: dict) -> None:
        """Expose the exact quotes the scanner consumed on the dashboard."""
        if self.dashboard is not None and hasattr(self.dashboard, "update_market"):
            try:
                self.dashboard.update_market(symbol, tick, candles, self.bridge.get_symbol_info(symbol))
            except TypeError:
                self.dashboard.update_market(symbol, tick, candles)
            except Exception:
                logger.debug("dashboard market feed failed", exc_info=True)

    def _feed(self, message: str):
        """Push a line to the terminal dashboard feed, if one is attached."""
        if self.dashboard is not None and hasattr(self.dashboard, "add_log"):
            try:
                self.dashboard.add_log(message)
            except Exception:
                logger.debug("dashboard feed failed", exc_info=True)
        logger.info(message)

    def _agent_feed(self, role: str, symbol: str, message: str):
        """Push a persona-attributed line to the agent activity panel."""
        line = f"[{role}] {symbol}: {message}"
        if self.dashboard is not None and hasattr(self.dashboard, "add_agent_log"):
            try:
                self.dashboard.add_agent_log(line)
            except Exception:
                logger.debug("dashboard agent feed failed", exc_info=True)
        logger.info(line)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="QuantScanner", daemon=True)
        self._thread.start()
        self.log_effective_safety_config()
        logger.info("Autonomous QuantScanner started.")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("Autonomous QuantScanner stopped.")

    def log_effective_safety_config(self):
        """Log the safety caps and approval gate in force for this process."""
        llm_cfg = getattr(self.cfg, "llm", None)
        trading = getattr(self.cfg, "trading", None)
        line = (
            f"SAFETY CONFIG: llm_require_approve={bool(getattr(llm_cfg, 'require_approve', getattr(self.cfg, 'llm_require_approve', False)))} "
            f"max_concurrent_positions={getattr(trading, 'max_concurrent_positions', None)} "
            f"max_positions_per_symbol={getattr(trading, 'max_positions_per_symbol', None)} "
            f"symbol_cooldown_seconds={getattr(trading, 'symbol_cooldown_seconds', None)} "
            f"max_lot={getattr(trading, 'max_lot', None)} "
            f"paper_trading={bool(getattr(self.cfg, 'paper_trading', False))} "
            f"require_telegram={bool(getattr(self.cfg, 'require_telegram', False))}"
        )
        self._feed(line)


    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self.scan_cycle()
            except Exception as e:
                logger.error(f"Error in scan cycle: {e}", exc_info=True)
            self._stop_event.wait(timeout=self.poll_interval)

    @staticmethod
    def _heartbeat_key(route) -> str:
        account = None
        getter = getattr(route, "get_account_info", None)
        if callable(getter):
            try:
                account = getter()
            except Exception:
                account = None
        login = getattr(account, "login", 0) if account else 0
        currency = getattr(account, "currency", "") if account else ""
        return f"{type(route).__name__}:{login}:{currency or getattr(route, 'account_currency', 'unknown')}"

    def check_bridge_heartbeat(self):
        """Alert independently when any routed venue feed freezes."""
        routes = {}
        symbols = getattr(self.cfg.trading, "symbols", None) or []
        for symbol in symbols:
            try:
                route = self.bridge.bridge_for(symbol) if hasattr(self.bridge, "bridge_for") else self.bridge
                routes[self._heartbeat_key(route)] = route
            except (KeyError, TypeError):
                continue
        if not routes:
            routes[self._heartbeat_key(self.bridge)] = self.bridge
        for key in routes:
            decision = evaluate_bridge_heartbeat(
                last_tick_epoch_s=self._last_heartbeat_tick_s.get(key),
                now_epoch_s=time.time(),
                threshold_s=float(getattr(self.cfg.trading, "bridge_heartbeat_timeout_s", 60.0)),
                previously_down=self._heartbeat_down.get(key, False),
            )
            if decision.healthy:
                if self._heartbeat_down.get(key, False):
                    self._feed(f"BRIDGE RECOVERED [{key}]: tick feed resumed")
                    logger.warning("Bridge heartbeat recovered [%s]", key)
                self._heartbeat_down[key] = False
                continue
            if decision.should_alert:
                self._heartbeat_down[key] = True
                message = f"BRIDGE ALERT [{key}]: {decision.reason}"
                self._feed(message)
                self._agent_feed("risk_manager", "BRIDGE", message)
                logger.error(message)
                if self.tg_controller and hasattr(self.tg_controller, "send_alert"):
                    try:
                        self.tg_controller.send_alert(f"<b>Vilona AI bridge alert</b>\n{message}")
                    except Exception as e:
                        logger.error(f"Failed to send bridge alert: {e}")

    def scan_cycle(self):
        symbols = getattr(self.cfg.trading, "symbols", None) or ["EURUSD"]
        symbols = [self._resolve_symbol(symbol) for symbol in symbols]
        self._feed(f"SCAN SYMBOL: {', '.join(symbols)}")

        self.persist_closed_trades()
        self._snapshot_open_positions()
        # Manage existing positions before checking the heartbeat. The first
        # cycle must observe a tick before it can report the bridge as healthy.
        self.manage_open_positions()
        for symbol in symbols:
            try:
                self.evaluate_symbol(symbol)
            except Exception as e:
                self._feed(f"ERROR {symbol}: {e}")
                logger.error(f"Failed scanning {symbol}: {e}")
        self.check_bridge_heartbeat()

    def _resolve_symbol(self, symbol: str) -> str:
        """Resolve a user-friendly symbol to the loaded CCXT contract symbol."""
        resolver = getattr(self.bridge, "resolve_symbol", None)
        return resolver(symbol) if resolver else symbol

    def manage_open_positions(self):
        """Manage open positions with optional LLM decisions and deterministic BE fallback."""
        try:
            positions = [] if bool(getattr(self.cfg, "paper_trading", False)) else (self.bridge.get_positions() or [])
            _owner = getattr(self.bridge, "bridge_for", None)
            llm_cfg = getattr(self.cfg, "llm", None)
            manager_enabled = bool(getattr(llm_cfg, "enabled", False))
            from core.order_safety import MAGIC_BASE, resolve_min_stop_distance
            flatten_min = int(getattr(self.cfg.trading, "weekend_flatten_min", 60))
            paper_mode = bool(getattr(self.cfg, "paper_trading", False))
            for pos in positions:
                if not pos or not pos.symbol:
                    continue
                # Weekend flatten applies only to Vilona-owned forex positions.
                # Crypto venues trade through the weekend, so nothing is
                # flattened there. In PAPER it records intent only.
                if (self._is_forex_symbol(pos.symbol)
                        and MAGIC_BASE <= int(getattr(pos, "magic", 0) or 0) < MAGIC_BASE + 1000):
                    owner = _owner(pos.symbol) if _owner else self.bridge
                    flatten_tick = owner.get_tick(pos.symbol)
                    flatten_epoch = (float(getattr(flatten_tick, "timestamp_ms", 0) or 0) / 1000.0
                                     if flatten_tick is not None else 0.0)
                    if is_weekend_flatten_window(server_epoch_s=flatten_epoch, flatten_min=flatten_min):
                        self._feed(f"WEEKEND FLATTEN {pos.symbol} ticket={pos.ticket}: weekend flatten")
                        logger.warning("Weekend flatten intent ticket=%s symbol=%s", pos.ticket, pos.symbol)
                        if paper_mode:
                            continue
                        try:
                            owner = _owner(pos.symbol) if _owner else self.bridge
                            result = owner.close_position(pos.ticket, pos.symbol, comment="weekend flatten", magic=MAGIC_BASE)
                            ok, msg = _trade_result(result)
                            if ok:
                                self._verify_position_action(pos, "CLOSE", None, None)
                            else:
                                self._feed(f"WEEKEND FLATTEN FAILED ticket={pos.ticket}: {msg}")
                        except Exception as exc:
                            self._feed(f"WEEKEND FLATTEN FAILED ticket={pos.ticket}: {exc}")
                            logger.exception("Weekend flatten failed for ticket=%s", pos.ticket)
                        continue
                owner = _owner(pos.symbol) if _owner else self.bridge
                candles = owner.get_candles(pos.symbol, "15m", limit=30)
                atr = compute_atr(candles, 14)
                if atr <= 0:
                    continue

                info = owner.get_symbol_info(pos.symbol) if hasattr(owner, "get_symbol_info") else None
                digits = _symbol_digits(info)
                dec = digits if digits is not None else 5

                # Hard loss guard is deterministic and never waits for LLM availability.
                open_px_val = float(pos.price_open or 0.0)
                cur_px_val = float(pos.price_current or 0.0)
                is_buy_val = pos.type == 0 or str(pos.type).lower() == "buy"
                loss_val = (cur_px_val - open_px_val) if is_buy_val else (open_px_val - cur_px_val)
                owned = MAGIC_BASE <= int(getattr(pos, "magic", 0) or 0) < MAGIC_BASE + 1000
                # Repair a missing broker-side stop before any LLM or trailing logic.
                if owned and float(getattr(pos, "sl", 0.0) or 0.0) <= 0:
                    stop_mult = float(getattr(self.cfg.trading, "emergency_stop_atr_mult", 1.5))
                    entry_candidate = open_px_val - stop_mult * atr if is_buy_val else open_px_val + stop_mult * atr
                    market_candidate = cur_px_val - stop_mult * atr if is_buy_val else cur_px_val + stop_mult * atr
                    entry_valid = entry_candidate < cur_px_val if is_buy_val else entry_candidate > cur_px_val
                    # Prefer a current-price anchored stop when it is on the
                    # protective side of both entry and market. Otherwise use
                    # the entry-anchored fallback, never a stop beyond the market.
                    current_valid = ((market_candidate < cur_px_val) if is_buy_val
                                     else (market_candidate > cur_px_val))
                    emergency_sl = market_candidate if current_valid else entry_candidate
                    emergency_valid = current_valid or entry_valid
                    min_stop = resolve_min_stop_distance(pos.symbol, getattr(self.cfg, "trading", None))
                    valid_side = emergency_sl < cur_px_val if is_buy_val else emergency_sl > cur_px_val
                    valid_distance = abs(cur_px_val - emergency_sl) >= min_stop
                    if digits is not None:
                        emergency_sl = round(emergency_sl, dec)
                    if emergency_valid and valid_side and valid_distance and hasattr(owner, "modify_position_sltp"):
                        try:
                            result = owner.modify_position_sltp(pos.ticket, pos.symbol, sl=emergency_sl, tp=pos.tp)
                            ok, msg = _trade_result(result)
                            self._feed(f"EMERGENCY SL {pos.symbol} ticket={pos.ticket}: {emergency_sl} {'applied' if ok else 'FAILED'}")
                            if not ok:
                                logger.error("Emergency SL failed ticket=%s: %s", pos.ticket, msg)
                        except Exception as exc:
                            self._feed(f"EMERGENCY SL FAILED ticket={pos.ticket}: {exc}")
                            logger.exception("Emergency SL failed for ticket=%s", pos.ticket)
                    else:
                        message = f"EMERGENCY SL INVALID {pos.symbol} ticket={pos.ticket}; closing unprotected position"
                        self._feed(message)
                        logger.error(message)
                        if not paper_mode:
                            try:
                                result = owner.close_position(pos.ticket, pos.symbol, comment="unprotected position")
                                ok, msg = _trade_result(result)
                                if not ok:
                                    self._feed(f"EMERGENCY CLOSE FAILED ticket={pos.ticket}: {msg}")
                            except Exception as exc:
                                self._feed(f"EMERGENCY CLOSE FAILED ticket={pos.ticket}: {exc}")
                    continue
                loss_limit = float(getattr(self.cfg.trading, "emergency_loss_atr_mult", 2.0)) * atr
                if owned and loss_limit > 0 and loss_val <= -loss_limit:
                    message = f"LOSS GUARD {pos.symbol} ticket={pos.ticket}: loss {abs(loss_val):.6g} >= {loss_limit:.6g}"
                    self._feed(message)
                    logger.error(message)
                    if not paper_mode and bool(getattr(self.cfg.trading, "emergency_close_on_loss", True)):
                        try:
                            result = owner.close_position(pos.ticket, pos.symbol, comment="emergency loss guard")
                            ok, msg = _trade_result(result)
                            self._record_position_decision(
                                ticket=pos.ticket, symbol=pos.symbol, action="CLOSE",
                                old_sl=pos.sl, old_tp=pos.tp, requested_sl=None, requested_tp=None,
                                result="SUCCESS" if ok else "FAILED", latency_ms=0,
                                error=None if ok else msg,
                            )
                            if ok:
                                self._verify_position_action(pos, "CLOSE", None, None)
                            else:
                                self._feed(f"LOSS GUARD CLOSE FAILED ticket={pos.ticket}: {msg}")
                        except Exception as exc:
                            self._feed(f"LOSS GUARD CLOSE FAILED ticket={pos.ticket}: {exc}")
                            logger.exception("Loss guard close failed for ticket=%s", pos.ticket)
                    continue
 

                now_ts = time.time()
                last_rev = self._last_position_review.get(pos.ticket, 0.0)
                can_review_llm = (now_ts - last_rev) >= 60.0

                # Save LLM latency/cost: positions that have not developed enough
                # (neither profit >= 30% of TP nor loss <= -0.8 ATR) are handled
                # purely by the deterministic safety ladder. The LLM is invoked
                # when there is an active decision: bank profit or cut loss early.
                open_px_val = float(pos.price_open or 0.0)
                cur_px_val = float(pos.price_current or 0.0)
                tp_val = float(pos.tp or 0.0)
                is_buy_val = pos.type == 0 or str(pos.type).lower() == "buy"
                tp_dist_val = (tp_val - open_px_val) if is_buy_val else (open_px_val - tp_val)
                gain_val = (cur_px_val - open_px_val) if is_buy_val else (open_px_val - cur_px_val)
                progress_val = (gain_val / tp_dist_val) if tp_dist_val > 0 else 0.0
                in_profit_zone = progress_val >= 0.30
                in_defense_zone = atr > 0 and gain_val <= -0.8 * atr
                in_llm_decision_zone = in_profit_zone or in_defense_zone
                if (manager_enabled and can_review_llm and in_llm_decision_zone
                        and not bool(getattr(self.cfg, "paper_trading", False))):
                    from core.order_safety import MAGIC_BASE
                    if MAGIC_BASE <= int(getattr(pos, "magic", 0) or 0) < MAGIC_BASE + 1000:
                        self._last_position_review[pos.ticket] = now_ts
                        try:
                            tick = owner.get_tick(pos.symbol)
                            if tick is not None:
                                snapshot = MarketSnapshot(
                                    symbol=pos.symbol, tick_bid=tick.bid, tick_ask=tick.ask,
                                    candles_15m=candles, candles_1h=[], spread_pct=((tick.ask-tick.bid)/tick.bid*100) if tick.bid else 0.0,
                                    atr_15m=atr, news_headlines=[],
                                    tick_value=float(getattr(info, "tick_value", 0.0) or 0.0),
                                    tick_size=float(getattr(info, "tick_size", 0.0) or 0.0),
                                )
                                snapshot.trend_tp_candidate = trend_tp_target(
                                    is_buy=is_buy_val, open_px=open_px_val, cur_px=cur_px_val,
                                    current_tp=tp_val, atr=atr,
                                    closes=[float(c.get("close", 0)) for c in candles if isinstance(c, dict)],
                                )
                                trend_candidate = snapshot.trend_tp_candidate
                                sl_val = float(pos.sl or 0.0)
                                risk_free = sl_val > 0 and ((sl_val >= open_px_val) if is_buy_val else (sl_val <= open_px_val))
                                if trend_candidate is not None and risk_free:
                                    current_tp_now = float(pos.tp or 0.0)
                                    trend_candidate = round(float(trend_candidate), dec) if digits is not None else float(trend_candidate)
                                    valid_tp = ((trend_candidate > cur_px_val and (current_tp_now <= 0 or trend_candidate > current_tp_now)) if is_buy_val else (trend_candidate < cur_px_val and (current_tp_now <= 0 or trend_candidate < current_tp_now)))
                                    if valid_tp:
                                        try:
                                            fast_result = owner.modify_position_sltp(pos.ticket, pos.symbol, sl=sl_val, tp=trend_candidate)
                                            fast_ok, fast_msg = _trade_result(fast_result)
                                            if fast_ok:
                                                pos.tp = trend_candidate
                                                self._feed(f"TREND TP {pos.symbol} ticket={pos.ticket}: {trend_candidate}")
                                            else:
                                                logger.warning("Deterministic trend TP failed ticket=%s: %s", pos.ticket, fast_msg)
                                        except Exception:
                                            logger.exception("Deterministic trend TP failed ticket=%s", pos.ticket)
                                from core.llm_manager import position_decision
                                from llm.client import get_llm_client
                                decision = position_decision(
                                    get_llm_client(), pos, snapshot,
                                    float(getattr(llm_cfg, "timeout", 30) or 30),
                                ) if self.judge_breaker.allow() else None
                            else:
                                decision = None
                        except Exception:
                            decision = None
                            logger.exception("LLM position manager failed for ticket=%s", pos.ticket)

                        if decision is not None:
                            self.judge_breaker.record_success()
                            action = decision["action"]
                            if action == "CLOSE":
                                started = time.time()
                                try:
                                    result = owner.close_position(pos.ticket, pos.symbol)
                                except Exception as exc:
                                    self._record_position_decision(
                                        ticket=pos.ticket, symbol=pos.symbol, action="CLOSE",
                                        old_sl=pos.sl, old_tp=pos.tp, requested_sl=None,
                                        requested_tp=None, result="FAILED",
                                        latency_ms=int((time.time() - started) * 1000),
                                        error=f"{type(exc).__name__}: {exc}",
                                    )
                                    self._feed(f"POSITION DECISION CLOSE FAILED ticket={pos.ticket}: {exc}")
                                    logger.exception("CLOSE failed for ticket=%s", pos.ticket)
                                    continue
                                ok, msg = _trade_result(result)
                                self._record_position_decision(
                                    ticket=pos.ticket, symbol=pos.symbol, action="CLOSE",
                                    old_sl=pos.sl, old_tp=pos.tp, requested_sl=None,
                                    requested_tp=None, result="SUCCESS" if ok else "FAILED",
                                    latency_ms=int((time.time() - started) * 1000),
                                    error=None if ok else msg,
                                )
                                self._agent_feed("risk_manager", pos.symbol, f"LLM CLOSE ticket={pos.ticket}: {decision['reason']} result={ok}")
                                if ok:
                                    self._verify_position_action(pos, "CLOSE", None, None)
                                else:
                                    self._feed(f"POSITION DECISION CLOSE FAILED ticket={pos.ticket}: {msg}")
                                continue
                            if action == "MODIFY":
                                is_buy = pos.type == 0 or str(pos.type).lower() == "buy"
                                open_px = float(pos.price_open)
                                cur_px = float(pos.price_current)
                                current_sl = float(pos.sl or 0.0)
                                current_tp = float(pos.tp or 0.0)
                                proposed_sl = float(decision.get("sl_price") or 0.0)
                                proposed_tp = float(decision.get("tp_price") or 0.0)
                                if proposed_sl > 0:
                                    proposed_sl = round(proposed_sl, dec)
                                if proposed_tp > 0:
                                    proposed_tp = round(proposed_tp, dec)

                                # SL: accept only a tighter stop on the correct side of price.
                                new_sl = current_sl
                                if proposed_sl > 0:
                                    tighter = (proposed_sl > current_sl) if is_buy else (current_sl == 0 or proposed_sl < current_sl)
                                    correct_side = proposed_sl < cur_px if is_buy else proposed_sl > cur_px
                                    if tighter and correct_side:
                                        new_sl = proposed_sl
                                # TP: extending the target is allowed only once the stop
                                # already protects entry, so a longer target can never turn
                                # an open loss into a bigger one. Capped at 6 ATR from entry.
                                new_tp = current_tp
                                if proposed_tp > 0 and proposed_tp != current_tp:
                                    risk_free = new_sl > 0 and ((new_sl >= open_px) if is_buy else (new_sl <= open_px))
                                    beyond_price = proposed_tp > cur_px if is_buy else proposed_tp < cur_px
                                    max_tp = open_px + 6.0 * atr if is_buy else open_px - 6.0 * atr
                                    within_cap = proposed_tp <= max_tp if is_buy else proposed_tp >= max_tp
                                    extends = (proposed_tp > current_tp) if is_buy else (current_tp == 0 or proposed_tp < current_tp)
                                    if beyond_price and (not extends or (risk_free and within_cap)):
                                        new_tp = proposed_tp

                                if new_sl != current_sl or new_tp != current_tp:
                                    started = time.time()
                                    try:
                                        result = owner.modify_position_sltp(pos.ticket, pos.symbol, sl=new_sl, tp=new_tp)
                                    except Exception as exc:
                                        self._record_position_decision(
                                            ticket=pos.ticket, symbol=pos.symbol, action="MODIFY",
                                            old_sl=pos.sl, old_tp=pos.tp, requested_sl=new_sl,
                                            requested_tp=new_tp, result="FAILED",
                                            latency_ms=int((time.time() - started) * 1000),
                                            error=f"{type(exc).__name__}: {exc}",
                                        )
                                        self._feed(f"POSITION DECISION MODIFY FAILED ticket={pos.ticket}: {exc}")
                                        logger.exception("MODIFY failed for ticket=%s", pos.ticket)
                                        continue
                                    ok, msg = _trade_result(result)
                                    self._record_position_decision(
                                        ticket=pos.ticket, symbol=pos.symbol, action="MODIFY",
                                        old_sl=pos.sl, old_tp=pos.tp, requested_sl=new_sl,
                                        requested_tp=new_tp, result="SUCCESS" if ok else "FAILED",
                                        latency_ms=int((time.time() - started) * 1000),
                                        error=None if ok else msg,
                                    )
                                    self._agent_feed("risk_manager", pos.symbol, f"LLM MODIFY ticket={pos.ticket} SL={new_sl} TP={new_tp}")
                                    if ok:
                                        self._verify_position_action(pos, "MODIFY", new_sl, new_tp)
                                    else:
                                        self._feed(f"POSITION DECISION MODIFY FAILED ticket={pos.ticket}: {msg}")
                                    continue
                        else:
                            self.judge_breaker.record_failure()

                if bool(getattr(self.cfg, "paper_trading", False)):
                    continue
                from core.order_safety import MAGIC_BASE
                if not (MAGIC_BASE <= int(getattr(pos, "magic", 0) or 0) < MAGIC_BASE + 1000):
                    continue
                open_px = float(pos.price_open)
                cur_px = float(pos.price_current)
                cur_sl = float(pos.sl) if pos.sl else 0.0
                is_buy = pos.type == 0 or str(pos.type).lower() == "buy"
                new_sl = profit_lock_sl(
                    is_buy=is_buy, open_px=open_px, cur_px=cur_px,
                    tp=float(pos.tp or 0.0), cur_sl=cur_sl, atr=atr, digits=dec,
                )
                if new_sl is not None:
                    min_stop = resolve_min_stop_distance(pos.symbol, getattr(self.cfg, "trading", None))
                    if min_stop > 0 and abs(cur_px - new_sl) < min_stop:
                        new_sl = None  # broker would reject a stop this close to price
                if new_sl is None or not hasattr(owner, "modify_position_sltp"):
                    continue
                started = time.time()
                try:
                    result = owner.modify_position_sltp(pos.ticket, pos.symbol, sl=new_sl, tp=pos.tp)
                except Exception as exc:
                    self._record_position_decision(
                        ticket=pos.ticket, symbol=pos.symbol, action="MODIFY",
                        old_sl=pos.sl, old_tp=pos.tp, requested_sl=new_sl,
                        requested_tp=pos.tp, result="FAILED",
                        latency_ms=int((time.time() - started) * 1000),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self._feed(f"POSITION DECISION MODIFY FAILED ticket={pos.ticket}: {exc}")
                    continue
                ok, msg = _trade_result(result)
                self._record_position_decision(
                    ticket=pos.ticket, symbol=pos.symbol, action="MODIFY",
                    old_sl=pos.sl, old_tp=pos.tp, requested_sl=new_sl,
                    requested_tp=pos.tp, result="SUCCESS" if ok else "FAILED",
                    latency_ms=int((time.time() - started) * 1000),
                    error=None if ok else msg,
                )
                if ok:
                    self._agent_feed("risk_manager", pos.symbol, f"PROFIT LOCK ticket={pos.ticket} SL {cur_sl} -> {new_sl}")
                    self._verify_position_action(pos, "MODIFY", new_sl, pos.tp)
                else:
                    self._feed(f"POSITION DECISION MODIFY FAILED ticket={pos.ticket}: {msg}")
        except Exception as e:
            logger.error(f"Error managing open positions: {e}")

    def _verify_position_action(self, pos, action: str, requested_sl, requested_tp):
        """Re-read broker state and audit any failed action readback."""
        try:
            positions = self.bridge.get_positions() or []
            current = next((p for p in positions if int(getattr(p, "ticket", -1)) == int(pos.ticket)), None)
            if action == "CLOSE":
                verified = current is None
            else:
                info = self.bridge.get_symbol_info(pos.symbol) if hasattr(self.bridge, "get_symbol_info") else None
                sym_digits = _symbol_digits(info)
                tol = (10.0 ** -sym_digits) * 1.5 if sym_digits is not None else 1e-4
                verified = current is not None and _close_enough(getattr(current, "sl", None), requested_sl, tol=tol) and _close_enough(getattr(current, "tp", None), requested_tp, tol=tol)
            if verified:
                return True
            error = f"{action} readback mismatch"
        except Exception as exc:
            verified = False
            error = f"{action} readback failed: {type(exc).__name__}: {exc}"
        self._record_position_decision(
            ticket=pos.ticket, symbol=pos.symbol, action=action,
            old_sl=pos.sl, old_tp=pos.tp, requested_sl=requested_sl,
            requested_tp=requested_tp, result="READBACK_FAILED",
            latency_ms=0, error=error,
        )
        self._feed(f"POSITION DECISION {action} READBACK FAILED ticket={pos.ticket}: {error}")
        logger.error("%s", error)
        return False

    @staticmethod
    def _risk_account_key(route) -> str:
        """Use venue plus broker login/currency so unrelated accounts never share drawdown."""
        venue = type(route).__name__
        getter = getattr(route, "get_account_info", None)
        account = getter() if callable(getter) else None
        login = getattr(account, "login", 0) if account else 0
        currency = getattr(account, "currency", "") if account else ""
        if login:
            return f"{venue}:{login}:{currency or 'unknown'}"
        return f"{venue}:{currency or getattr(route, 'account_currency', 'unknown')}"

    def evaluate_symbol(self, symbol: str):
        # Global capacity check: count the whole book, not just this symbol.
        # Counting per symbol let 4 symbols each hold max_concurrent_positions,
        # which is how 13 positions accumulated against a cap of 5.
        # PAPER never reads the broker book: without credentials there is no
        # private endpoint, and the simulated order is latched locally anyway.
        paper_mode = bool(getattr(self.cfg, "paper_trading", False))
        route = self.bridge.bridge_for(symbol) if hasattr(self.bridge, "bridge_for") else self.bridge
        all_positions = [] if paper_mode else (route.get_positions() if hasattr(route, "get_positions") else [])
        if len(all_positions) >= self.cfg.trading.max_concurrent_positions:
            self._feed(
                f"{symbol}: skipped — {len(all_positions)} open positions "
                f"at cap {self.cfg.trading.max_concurrent_positions}"
            )
            return

        # The whole book is already at capacity; a per-symbol count is redundant.
        open_positions = [p for p in all_positions if getattr(p, "symbol", None) == symbol]

        # Per-symbol cap: five sequential XAUUSD entries accumulated because the
        # global cap was the only limit. One position per symbol per setup.
        per_symbol_cap = getattr(self.cfg.trading, "max_positions_per_symbol", 0) or 0
        if per_symbol_cap > 0 and len(open_positions) >= per_symbol_cap:
            self._feed(
                f"{symbol}: skipped — {len(open_positions)} open position(s) "
                f"at per-symbol cap {per_symbol_cap}"
            )
            return

        # Per-symbol cooldown prevents repeated entries on the same setup.
        cooldown = getattr(self.cfg.trading, "symbol_cooldown_seconds", 0) or 0
        last_entry = self._last_entry_at.get(symbol, 0.0)
        if cooldown > 0 and last_entry > 0 and (time.time() - last_entry) < cooldown:
            remaining = int(cooldown - (time.time() - last_entry))
            self._feed(f"{symbol}: cooldown active, {remaining}s remaining")
            return
        # Repeated broker rejections indicate a symbol-level execution problem.
        # Fail closed after three rejections in the trailing hour; the operator
        # must inspect the persisted reasons before retrying that symbol.
        if self.db is not None and hasattr(self.db, "count_recent_rejections"):
            rejection_count = self.db.count_recent_rejections(symbol, 3600.0)
            if rejection_count >= 3:
                message = (
                    f"SYMBOL REJECTION BLOCK {symbol}: "
                    f"{rejection_count} rejections in 1h"
                )
                self._feed(message)
                logger.error(message)
                if self.tg_controller and hasattr(self.tg_controller, "send_alert"):
                    try:
                        self.tg_controller.send_alert(message)
                    except Exception:
                        logger.exception("Failed to alert rejection block for %s", symbol)
                return

        tick = route.get_tick(symbol)
        if not tick or tick.bid <= 0 or tick.ask <= 0:
            self._feed(f"{symbol}: no tick")
            return

        spread_pct = ((tick.ask - tick.bid) / tick.bid) * 100.0 if tick.bid > 0 else 0.0
        if spread_pct > max(self.cfg.trading.max_spread_pct_default, self.cfg.trading.max_spread_pct):
            self._feed(f"{symbol}: spread {spread_pct:.4f}% too wide")
            return
        # Stale-tick gate: a frozen feed can present a plausible price while the
        # market has moved. Without this the scanner can size and enter on an
        # arbitrarily old quote.
        tick_epoch_s = float(getattr(tick, "timestamp_ms", 0) or 0) / 1000.0
        freshness = evaluate_tick_freshness(
            tick_timestamp_s=tick_epoch_s,
            now_s=time.time(),
            max_age_s=float(getattr(self.cfg.trading, "max_tick_age_seconds", 30.0)),
        )
        if not freshness.allowed:
            self._feed(f"ENTRY LOCK {symbol}: {freshness.reason}")
            logger.warning("Stale tick blocked %s: %s", symbol, freshness.reason)
            return
        # Any accepted tick proves only this routed bridge is alive.
        heartbeat_key = self._heartbeat_key(route)
        self._last_heartbeat_tick_s[heartbeat_key] = time.time()

        # Weekend guard uses broker tick time, never the host clock: the host may
        # sit in a different timezone and would otherwise allow Friday pre-close entries.
        # Crypto venues (CCXT: Binance, Bybit, ...) never close, so the forex
        # entry and flatten windows do not apply to them.
        if self._is_forex_symbol(symbol):
            weekend = evaluate_weekend_guard(
                server_epoch_s=tick_epoch_s,
                entry_cutoff_min=int(getattr(self.cfg.trading, "weekend_entry_cutoff_min", 120)),
                open_grace_min=int(getattr(self.cfg.trading, "week_open_grace_min", 30)),
            )
            if not weekend.allowed:
                self._feed(f"ENTRY LOCK {symbol}: {weekend.reason}")
                logger.warning("Weekend guard blocked %s: %s", symbol, weekend.reason)
                return

        candle_time = 0
        strategy = getattr(self.cfg.trading, "strategy", "pullback")
        if strategy == "scalp_5m":
            c15m = route.get_candles(symbol, "15m", limit=60)
            c5m = route.get_candles(symbol, "5m", limit=60)
            c1h = route.get_candles(symbol, "1h", limit=60)
            if len(c15m) < 50 or len(c5m) < 20:
                self._feed(f"{symbol}: insufficient scalp candles (15m={len(c15m)}, 5m={len(c5m)})")
                return
            band_mult = float(getattr(self.cfg.trading, "scalp_pullback_band", 1.5))
            action, confidence, reason, atr_signal = compute_scalp_5m_signal(
                c15m, c5m, float(tick.bid), band_mult, c1h=c1h
            )
            candle_time = c15m[-1].get("time") if c15m else 0
            self._publish_market_data(
                symbol, tick, {"5m": c5m[-1], "15m": c15m[-1], "1h": c1h[-1] if c1h else {}}
            )
        elif strategy == "breakout":
            c15m = route.get_candles(symbol, "15m", limit=60)
            c1h = route.get_candles(symbol, "1h", limit=80)
            if len(c1h) < 50 or len(c15m) < 21:
                self._feed(f"{symbol}: insufficient breakout candles (15m={len(c15m)}, 1h={len(c1h)})")
                return
            action, confidence, reason, atr_signal = compute_breakout_signal(
                c15m, c1h, float(tick.bid), int(getattr(self.cfg.trading, "breakout_lookback", 20))
            )
            candle_time = c15m[-1].get("time") if c15m else 0
            self._publish_market_data(
                symbol, tick, {"5m": c15m[-1], "15m": c15m[-1], "1h": c1h[-1]}
            )
        else:
            c15m = route.get_candles(symbol, "15m", limit=60)
            c1h = route.get_candles(symbol, "1h", limit=80)
            if len(c1h) < 50 or len(c15m) < 20:
                self._feed(f"{symbol}: insufficient candles (15m={len(c15m)}, 1h={len(c1h)})")
                return

            atr_15m = compute_atr(c15m, 14)
            atr_signal = atr_15m
            closes_1h = [float(x.get("close", 0)) for x in c1h]
            ema21 = compute_ema(closes_1h, 21)
            ema50 = compute_ema(closes_1h, 50)
            cur_price = float(tick.bid)

            atr_series: List[float] = []
            for end in range(len(c15m) - 1, max(len(c15m) - 31, 14), -1):
                window = c15m[: end + 1]
                a = compute_atr(window, 14)
                if a > 0:
                    atr_series.append(a)

            action = "WAIT"
            confidence = 0.0
            reason = ""

            if cur_price > ema21 and ema21 > ema50:
                distance_to_ema21 = cur_price - ema21
                if distance_to_ema21 <= (1.2 * atr_15m):
                    action = "BUY"
                    confidence = compute_setup_confidence(
                        ema21, ema50, cur_price, atr_15m, atr_series, bullish=True
                    )
                    reason = (
                        f"Bullish Trend: Price {cur_price:.5f} > EMA21 {ema21:.5f} > "
                        f"EMA50 {ema50:.5f}, pullback {distance_to_ema21 / atr_15m:.2f} ATR, "
                        f"gradient {(ema21 - ema50) / atr_15m:.2f} ATR"
                    )
            elif cur_price < ema21 and ema21 < ema50:
                distance_to_ema21 = ema21 - cur_price
                if distance_to_ema21 <= (1.2 * atr_15m):
                    action = "SELL"
                    confidence = compute_setup_confidence(
                        ema21, ema50, cur_price, atr_15m, atr_series, bullish=False
                    )
                    reason = (
                        f"Bearish Trend: Price {cur_price:.5f} < EMA21 {ema21:.5f} < "
                        f"EMA50 {ema50:.5f}, pullback {distance_to_ema21 / atr_15m:.2f} ATR, "
                        f"gradient {(ema50 - ema21) / atr_15m:.2f} ATR"
                    )
            candle_time = c15m[-1].get("time") if c15m else 0
            self._publish_market_data(
                symbol, tick, {"5m": c15m[-1], "15m": c15m[-1], "1h": c1h[-1]}
            )

        if action == "WAIT":
            self._feed(f"{symbol}: no signal")
            self._agent_feed("trader", symbol, f"WAIT — {reason or 'no qualifying setup'}")
            return

        setup_key = "|".join(str(value) for value in (symbol, action, candle_time))
        if (
            self.db is not None
            and hasattr(self.db, "has_order_setup")
            and self.db.has_order_setup(setup_key)
        ):
            self._feed(f"{symbol}: persisted duplicate setup skipped")
            return
        same_setup = self._last_setup_key.get(symbol) == setup_key
        cooldown_active = (
            cooldown <= 0
            or (last_entry > 0 and (time.time() - last_entry) < cooldown)
        )
        if same_setup and cooldown_active:
            self._feed(f"{symbol}: duplicate setup skipped")
            return

        self._agent_feed("trader", symbol, f"{action} signal confidence={confidence:.2f}: {reason}")

        signal = Signal(
            symbol=symbol,
            action=action,
            confidence=confidence,
            reason=reason,
            entry_hint=tick.ask if action == "BUY" else tick.bid,
            source_role="trader",
        )

        symbol_info = None
        if hasattr(route, "get_symbol_info"):
            symbol_info = route.get_symbol_info(symbol)

        snapshot = MarketSnapshot(
            symbol=symbol,
            tick_bid=tick.bid,
            tick_ask=tick.ask,
            candles_15m=c15m,
            candles_1h=c1h,
            spread_pct=spread_pct,
            atr_15m=atr_signal,
            news_headlines=[],
            tick_value=float(getattr(symbol_info, "tick_value", 0.0) or 0.0),
            tick_size=float(getattr(symbol_info, "tick_size", 0.0) or 0.0),
        )

        # Equity must come from the broker; a fabricated fallback would make
        # percentage loss limits appear valid when account data is unavailable.
        equity = None
        free_margin = None
        route = self.bridge.bridge_for(symbol) if hasattr(self.bridge, "bridge_for") else self.bridge
        if hasattr(route, "get_account_info"):
            acc = route.get_account_info()
            if acc:
                broker_equity = getattr(acc, "equity", None)
                if broker_equity is not None:
                    equity = float(broker_equity)
                if getattr(acc, "free_margin", None) is not None:
                    free_margin = float(acc.free_margin)

        # Fail closed before risk/judge/order when operator stop or authoritative
        # broker-day realized P/L is missing/over limit. This affects entries only.
        kill_switch = self._kill_switch.is_active()
        guard_daily_pl_usd = daily_pl_from_bridge_day(route, now_epoch=time.time()) if not kill_switch else None
        guard = evaluate_entry_lock(
            kill_switch=kill_switch,
            daily_pl_usd=guard_daily_pl_usd,
            equity_usd=equity,
            max_daily_loss_pct=float(getattr(self.cfg.trading, "max_daily_loss_pct", 0.0)),
        )
        if not guard.allowed:
            self._feed(f"ENTRY LOCK {symbol}: {guard.reason}")
            logger.warning("Entry lock blocked %s: %s", symbol, guard.reason)
            return
        # Total drawdown and consecutive-loss locks require their StateDB protocol.
        get_start_balance = getattr(self.db, "get_start_balance", None)
        if callable(get_start_balance):
            account = self._risk_account_key(route)
            set_start_balance = getattr(self.db, "set_start_balance", None)
            is_drawdown_latched = getattr(self.db, "is_drawdown_latched", lambda **kwargs: False)
            latch_drawdown = getattr(self.db, "latch_drawdown", lambda **kwargs: None)
            if get_start_balance(account) is None and equity is not None and callable(set_start_balance):
                set_start_balance(equity, account_login=0, account=account)
            drawdown = evaluate_total_drawdown(
                equity_usd=equity,
                start_balance_usd=get_start_balance(account),
                max_drawdown_pct=float(getattr(self.cfg.trading, "max_total_drawdown_pct", 0.0)),
                latched=bool(is_drawdown_latched(account)),
            )
            if not drawdown.allowed:
                if drawdown.reason == "total drawdown threshold reached":
                    latch_drawdown(account=account)
                self._feed(f"ENTRY LOCK {symbol}: {drawdown.reason}")
                logger.warning("Entry lock blocked %s: %s", symbol, drawdown.reason)
                return

        get_trades = getattr(self.db, "get_trades", None)
        if callable(get_trades):
            is_loss_latched = getattr(self.db, "is_consecutive_loss_latched", lambda: False)
            latch_loss = getattr(self.db, "latch_consecutive_loss", lambda: None)
            get_setting = getattr(self.db, "get_setting", lambda key: None)
            try:
                reset_trade_id = int(get_setting("risk_latches_reset_trade_id") or 0)
            except (TypeError, ValueError):
                reset_trade_id = 0
            losses = evaluate_consecutive_losses(
                loss_count=consecutive_loss_count(get_trades(limit=1000), after_trade_id=reset_trade_id),
                max_losses=int(getattr(self.cfg.trading, "max_consecutive_losses", 0)),
                latched=bool(is_loss_latched()),
            )
            if not losses.allowed:
                if losses.reason == "consecutive loss threshold reached":
                    latch_loss()
                self._feed(f"ENTRY LOCK {symbol}: {losses.reason}")
                logger.warning("Entry lock blocked %s: %s", symbol, losses.reason)
                return

        # All open positions, not just this symbol's, so exposure caps see the whole book.
        all_positions = []
        if hasattr(route, "get_positions"):
            all_positions = route.get_positions() or []

        # Risk Manager Evaluation
        verdict = risk_evaluate(
            signal=signal,
            snapshot=snapshot,
            equity_usd=equity,
            cfg=self.cfg.trading,
            daily_pl_usd=guard_daily_pl_usd,
            # The global cap counts the whole book; the per-symbol cap is enforced
            # earlier in evaluate_symbol. Passing this symbol's count here compared
            # it against the global limit and broke once stacking was allowed.
            open_positions_count=len(all_positions),
            free_margin=free_margin,
            open_positions=all_positions,
        )

        if not verdict.approved:
            self._feed(f"RISK REJECT {symbol}: {verdict.reason}")
            self._agent_feed("risk_manager", symbol, f"REJECT — {verdict.reason}")
            logger.info(f"Risk Manager rejected {symbol} signal: {verdict.reason}")
            return
        digits = _symbol_digits(symbol_info)
        if digits is not None:
            if verdict.sl_price is not None:
                verdict.sl_price = round(verdict.sl_price, digits)
            if verdict.tp_price is not None:
                verdict.tp_price = round(verdict.tp_price, digits)
        self._agent_feed(
            "risk_manager",
            symbol,
            f"APPROVE lot={verdict.lot_size} SL={verdict.sl_price} TP={verdict.tp_price} R:R={verdict.rr_ratio:.2f}",
        )

        from core.order_safety import (
            place_protected_order,
            resolve_magic,
            resolve_min_stop_distance,
        )

        llm_cfg = getattr(self.cfg, "llm", None)
        judge_started = time.monotonic()

        def record_judge(verdict_value, reason_value, error_value=None):
            """Persist every judge outcome so the paper soak is auditable."""
            if self.db is None:
                return
            try:
                self.db.save_judge_decision({
                    "symbol": symbol,
                    "action": action,
                    "confidence": confidence,
                    "risk_approved": True,
                    "lot_size": verdict.lot_size,
                    "sl": verdict.sl_price,
                    "tp": verdict.tp_price,
                    "verdict": verdict_value,
                    "reason": reason_value,
                    "latency_ms": int((time.monotonic() - judge_started) * 1000),
                    "error": error_value,
                    "model": getattr(llm_cfg, "model", None),
                })
            except Exception as e:
                logger.warning(f"failed to persist judge decision for {symbol}: {e}")


        manager_enabled = bool(getattr(llm_cfg, "enabled", False))
        # The LLM manager did not produce a usable approval for this entry.
        manager_error = False
        if manager_enabled:
            if not self.judge_breaker.allow():
                manager_error = True
                record_judge("ERROR", f"circuit open ({self.judge_breaker.state()})", "CircuitOpenError")
                self._feed(f"LLM MANAGER unavailable for {symbol}; using deterministic fallback")
            else:
                from core.llm_manager import entry_decision
                from llm.client import get_llm_client
                decision = entry_decision(
                    get_llm_client(), signal, verdict, snapshot,
                    {"equity": equity, "free_margin": free_margin, "open_positions": len(all_positions)},
                    float(getattr(llm_cfg, "timeout", 30) or 30),
                )
                if decision is None:
                    manager_error = True
                    self.judge_breaker.record_failure()
                    record_judge("ERROR", "manager returned invalid/empty response; deterministic fallback", "LLMError")
                else:
                    self.judge_breaker.record_success()
                    manager_action = decision["action"]
                    manager_reason = decision["reason"] or "no reason supplied"
                    if manager_action == "VETO":
                        record_judge("VETO", manager_reason)
                        self._agent_feed("judge", symbol, f"VETO — {manager_reason}")
                        return
                    if manager_action == "ADJUST":
                        try:
                            requested_lot = float(decision["lot_size"])
                            requested_sl = float(decision["sl_price"])
                            requested_tp = float(decision["tp_price"])
                            entry = float(signal.entry_hint or (tick.ask if action == "BUY" else tick.bid))
                            is_buy = action == "BUY"
                            original_stop_distance = abs(entry - float(verdict.sl_price))
                            adjusted_stop_distance = abs(entry - requested_sl)
                            adjusted_rr = abs(requested_tp - entry) / adjusted_stop_distance if adjusted_stop_distance > 0 else 0.0
                            tighter_stop = requested_sl >= float(verdict.sl_price) if is_buy else requested_sl <= float(verdict.sl_price)
                            valid = (
                                requested_lot > 0 and requested_lot <= verdict.lot_size
                                and requested_sl > 0 and requested_tp > 0
                                and (requested_sl < entry < requested_tp if is_buy else requested_tp < entry < requested_sl)
                                and tighter_stop and adjusted_stop_distance <= original_stop_distance
                                and adjusted_rr >= 1.5
                                and adjusted_stop_distance >= resolve_min_stop_distance(symbol, self.cfg.trading)
                            )
                        except (TypeError, ValueError):
                            valid = False
                        if not valid:
                            manager_error = True
                            self.judge_breaker.record_failure()
                            record_judge("ERROR", "manager proposed invalid adjustment; deterministic fallback", "InvalidManagerAction")
                        else:
                            verdict.lot_size = requested_lot
                            verdict.sl_price = requested_sl
                            verdict.tp_price = requested_tp
                            verdict.rr_ratio = adjusted_rr
                            record_judge("APPROVE", f"adjusted: {manager_reason}")
                    else:
                        record_judge("APPROVE", manager_reason)

        require_approve = bool(getattr(llm_cfg, "require_approve", getattr(self.cfg, "llm_require_approve", True)))
        if manager_error and require_approve:
            # Fail closed: no manager approval means no order.
            record_judge("ERROR", "blocked: approval required", "ApprovalRequired")
            self._feed(f"ENTRY LOCK {symbol}: blocked: approval required")
            return

        if not manager_enabled and verdict.rr_ratio < 1.5:
            record_judge("VETO", f"R:R ratio {verdict.rr_ratio:.2f} below 1.5 minimum")
            self._feed(f"JUDGE VETO {symbol}: R:R ratio {verdict.rr_ratio} < 1.5")
            self._agent_feed("judge", symbol, f"VETO — R:R {verdict.rr_ratio:.2f} below 1.5 minimum")
            return

        self._agent_feed(
            "judge",
            symbol,
            f"CLEAR — R:R {verdict.rr_ratio:.2f} acceptable for execution",
        )

        # Execution

        # PAPER MODE: never touch the execution bridge. Record the intent, then
        # stop. Without this guard the PAPER label was cosmetic and orders went
        # to the live bridge.
        if bool(getattr(self.cfg, "paper_trading", False)):
            ref_price = signal.entry_hint or (tick.ask if action == "BUY" else tick.bid)
            self._feed(
                f"PAPER {action} {symbol}: lot={verdict.lot_size}, SL={verdict.sl_price}, "
                f"TP={verdict.tp_price}, R:R={verdict.rr_ratio:.2f} — simulated, no order sent"
            )
            self._agent_feed(
                "trader", symbol,
                f"PAPER {action} @ {ref_price} lot={verdict.lot_size} R:R={verdict.rr_ratio:.2f} — no order sent",
            )
            if self.db is not None:
                try:
                    inserted = self.db.save_order({
                        "ticket": None,
                        "symbol": symbol,
                        "type": "paper",
                        "side": action.lower(),
                        "quantity": verdict.lot_size,
                        "price": ref_price,
                        "sl": verdict.sl_price,
                        "tp": verdict.tp_price,
                        "setup_key": setup_key,
                    })
                    if not inserted:
                        self._feed(f"{symbol}: concurrent duplicate setup skipped")
                        return
                except Exception as e:
                    logger.warning(f"Failed to persist paper trade for {symbol}: {e}")
            logger.info(
                f"PAPER MODE: {action} {symbol} latched inside scanner; "
                f"cooldown armed, no bridge call"
            )
            self._last_entry_at[symbol] = time.time()
            self._last_setup_key[symbol] = setup_key
            return

        self._feed(f"EXECUTING {action} {symbol}: lot={verdict.lot_size}, SL={verdict.sl_price}, TP={verdict.tp_price}")
        if self.db is not None:
            try:
                inserted = self.db.save_order({
                    "ticket": None,
                    "symbol": symbol,
                    "type": "market",
                    "side": action.lower(),
                    "quantity": verdict.lot_size,
                    "price": signal.entry_hint or (tick.ask if action == "BUY" else tick.bid),
                    "sl": verdict.sl_price,
                    "tp": verdict.tp_price,
                    "status": "submitting",
                    "created_at": time.time(),
                    "setup_key": setup_key,
                })
                if not inserted:
                    self._feed(f"{symbol}: concurrent duplicate setup skipped")
                    return
            except Exception:
                logger.exception("Failed to reserve setup intent for %s; refusing broker call", symbol)
                return

        protected = place_protected_order(
            route,
            symbol=symbol,
            action=action.lower(),
            volume=verdict.lot_size,
            sl=verdict.sl_price,
            tp=verdict.tp_price,
            magic=resolve_magic(symbol),
            comment="vilona-auto",
            reference_price=signal.entry_hint or (tick.ask if action == "BUY" else tick.bid),
            min_stop_distance=resolve_min_stop_distance(symbol, self.cfg.trading),
            digits=_symbol_digits(symbol_info),
        )
        trade_result = protected if protected.success else None

        if not protected.success:
            # Rejected attempts are recorded too; they must not arm cooldown.
            reason = protected.message or "unknown error"
            if self.db is not None:
                try:
                    self.db.update_order_setup(
                        setup_key,
                        ticket=None,
                        status="rejected",
                        rejection_reason=reason,
                    )
                except Exception:
                    logger.exception("Failed to update rejected setup %s", setup_key)
            self._feed(f"ORDER REJECTED {symbol}: {reason}")
            self._agent_feed("risk_manager", symbol, f"ORDER REJECTED — {reason}")
            logger.error(f"Order not placed/protected for {symbol}: {reason}")
            return

        if trade_result and trade_result.ticket:
            self._feed(f"ORDER SUCCESS {symbol} Ticket #{trade_result.ticket}")
            logger.info(f"Successfully placed order {symbol} Ticket #{trade_result.ticket}")
            # Notify Telegram
            if self.tg_controller and hasattr(self.tg_controller, "send_alert"):
                msg = (
                    f"🚀 <b>Vilona AI Trade Alert</b>\n"
                    f"Symbol: {symbol}\n"
                    f"Action: {action}\n"
                    f"Lot Size: {verdict.lot_size}\n"
                    f"Entry: {signal.entry_hint}\n"
                    f"SL: {verdict.sl_price}\n"
                    f"TP: {verdict.tp_price}\n"
                    f"R:R: {verdict.rr_ratio}\n"
                    f"Ticket: #{trade_result.ticket}"
                )
                try:
                    self.tg_controller.send_alert(msg)
                except Exception as e:
                    logger.error(f"Failed to send Telegram alert: {e}")

            # The durable reservation created before broker submission is the audit row.
            if self.db is not None:
                try:
                    self.db.update_order_setup(
                        setup_key, ticket=int(trade_result.ticket), status="filled"
                    )
                    self._last_entry_at[symbol] = time.time()
                    self._last_setup_key[symbol] = setup_key
                except Exception:
                    logger.exception("Failed to update successful setup %s", setup_key)
        else:
            # Rejected attempts are recorded too; they must not arm cooldown.
            try:
                self.db.save_order({
                    "ticket": None,
                    "symbol": symbol,
                    "type": "market",
                    "side": action.lower(),
                    "quantity": verdict.lot_size,
                    "price": signal.entry_hint or (tick.ask if action == "BUY" else tick.bid),
                    "sl": verdict.sl_price,
                    "tp": verdict.tp_price,
                    "status": "rejected",
                    "created_at": time.time(),
                })
            except Exception as e:
                logger.warning(f"failed to save rejected order to db: {e}")
            self._feed(f"ORDER REJECTED {symbol}: {getattr(trade_result, 'message', 'unknown error')}")
