"""Deterministic trade telemetry: session tagging and side/session aggregates.

The paper-soak review needs the same accounting as the broker, so every number
here is derived from persisted closed-trade rows instead of positions or logs.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# Session windows are UTC-hour based and deliberately non-overlapping so every
# trade lands in exactly one bucket: London 07-12, New York 12-21, else off-hours.
SESSION_LONDON = "london"
SESSION_NEW_YORK = "new_york"
SESSION_OFF_HOURS = "off_hours"

_EMPTY = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "win_rate": 0.0,
    "gross_profit": 0.0,
    "gross_loss": 0.0,
    "costs": 0.0,
    "net_profit": 0.0,
    "expectancy": 0.0,
    "profit_factor": 0.0,
}


def classify_session(epoch_seconds: Optional[float]) -> str:
    """Map a close timestamp onto a trading session bucket (UTC)."""
    if not epoch_seconds:
        return SESSION_OFF_HOURS
    hour = int((float(epoch_seconds) // 3600) % 24)
    if 7 <= hour < 12:
        return SESSION_LONDON
    if 12 <= hour < 21:
        return SESSION_NEW_YORK
    return SESSION_OFF_HOURS


def net_pnl(trade: Dict[str, Any]) -> float:
    """Net P/L: gross plus swap minus commission, all broker-reported when present."""
    profit = _as_float(trade.get("profit"))
    commission = abs(_as_float(trade.get("commission")))
    swap = _as_float(trade.get("swap"))
    return profit + swap - commission


def classify_outcome(trade: Dict[str, Any]) -> str:
    """Label a closed trade by which protection level was reached."""
    explicit = trade.get("outcome")
    if explicit:
        return str(explicit)
    return "win" if net_pnl(trade) > 0 else "loss"


def summarize_trades(trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate closed trades into the review metrics for one bucket."""
    rows = list(trades)
    if not rows:
        return dict(_EMPTY)
    wins, losses, gross_profit, gross_loss, costs, net = 0, 0, 0.0, 0.0, 0.0, 0.0
    for trade in rows:
        pnl = net_pnl(trade)
        net += pnl
        costs += abs(_as_float(trade.get("commission"))) + abs(_as_float(trade.get("swap")))
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        else:
            losses += 1
            gross_loss += abs(pnl)
    total = len(rows)
    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100.0, 2),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "costs": round(costs, 4),
        "net_profit": round(net, 4),
        "expectancy": round(net / total, 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else 0.0,
    }


def build_trade_aggregates(trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Group closed trades by side and by session, plus the overall bucket."""
    rows = list(trades)
    for trade in rows:
        if not trade.get("session"):
            trade["session"] = classify_session(trade.get("closed_at"))
    sides = {}
    for side in ("buy", "sell"):
        sides[side] = summarize_trades([t for t in rows if str(t.get("side", "")).lower() == side])
    sessions = {}
    for session in (SESSION_LONDON, SESSION_NEW_YORK, SESSION_OFF_HOURS):
        sessions[session] = summarize_trades([t for t in rows if t.get("session") == session])
    return {"overall": summarize_trades(rows), "by_side": sides, "by_session": sessions}


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
