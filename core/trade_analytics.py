"""Outcome statistics for closed trades. All P/L values are account-currency units."""
from __future__ import annotations

from typing import Any, Iterable


def closed_trade_metrics(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Calculate outcomes only from closed trades with known profit.

    Net outcome includes profit, commission and swap (each defaulting to zero if
    missing). Drawdown is the peak-to-trough decline of cumulative net P/L,
    ordered by closed_at ascending; no initial account balance is assumed.
    """
    closed = [
        row for row in trades
        if str(row.get("status", "")).lower() == "closed"
        and row.get("profit") is not None
    ]
    closed.sort(key=lambda row: (float(row.get("closed_at") or 0), int(row.get("id") or 0)))
    outcomes = [
        float(row["profit"]) + float(row.get("commission") or 0) + float(row.get("swap") or 0)
        for row in closed
    ]
    wins = [x for x in outcomes if x > 0]
    losses = [x for x in outcomes if x < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    cumulative = peak = max_drawdown = 0.0
    for outcome in outcomes:
        cumulative += outcome
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "closed_trade_count": len(outcomes),
        "win_rate": len(wins) / len(outcomes) if outcomes else None,
        "expectancy": sum(outcomes) / len(outcomes) if outcomes else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else None),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": sum(outcomes),
        "max_drawdown": max_drawdown,
    }
