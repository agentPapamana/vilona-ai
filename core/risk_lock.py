"""Fail-closed entry lock for explicit stop and daily realized-loss limits."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RiskLockDecision:
    allowed: bool
    reason: str


def evaluate_entry_lock(*, kill_switch: bool, daily_pl_usd: float | None,
                        equity_usd: float | None, max_daily_loss_pct: float) -> RiskLockDecision:
    """Allow entries only when not stopped and authoritative inputs are present."""
    if kill_switch:
        return RiskLockDecision(False, "operator kill switch active")
    if daily_pl_usd is None or equity_usd is None:
        return RiskLockDecision(False, "daily P/L or equity unavailable")
    if not math.isfinite(daily_pl_usd) or not math.isfinite(equity_usd) or not math.isfinite(max_daily_loss_pct):
        return RiskLockDecision(False, "invalid non-finite risk input")
    if equity_usd <= 0 or max_daily_loss_pct <= 0:
        return RiskLockDecision(False, "invalid equity or loss threshold")
    loss_limit = equity_usd * max_daily_loss_pct / 100.0
    if daily_pl_usd <= -loss_limit:
        return RiskLockDecision(False, "daily loss threshold reached")
    return RiskLockDecision(True, "within daily loss threshold")


def bridge_daily_pl_usd(bridge, *, since_epoch: float) -> float | None:
    """Read realized P/L from a bridge history protocol; missing data is None.

    Bridge method contract: ``get_history_deals(since_epoch)`` returns iterable
    mappings with profit and optional commission/swap account-currency amounts.
    The scanner must not treat ``None`` as zero; it is a fail-closed condition.
    """
    getter = getattr(bridge, "get_history_deals", None)
    if not callable(getter):
        return None
    try:
        deals = getter(since_epoch)
        if deals is None:
            return None
        total = 0.0
        for deal in deals:
            if not isinstance(deal, dict) or deal.get("profit") is None:
                return None
            total += float(deal["profit"])
            total += float(deal.get("commission") or 0.0)
            total += float(deal.get("swap") or 0.0)
            total += float(deal.get("fee") or 0.0)
        import math
        return total if math.isfinite(total) else None
    except Exception:
        return None


def daily_pl_from_bridge_day(bridge, *, now_epoch: float) -> float | None:
    """Get P/L using the bridge's explicit server-day boundary; None means unavailable."""
    since = get_server_day_start(bridge, now_epoch=now_epoch)
    return bridge_daily_pl_usd(bridge, since_epoch=since) if since is not None else None


def entry_lock_from_bridge(bridge, *, kill_switch: bool, now_epoch: float,
                           equity_usd: float | None, max_daily_loss_pct: float) -> RiskLockDecision:
    """Resolve broker-day boundary and realized P/L, then fail closed if unavailable."""
    daily_pl = daily_pl_from_bridge_day(bridge, now_epoch=now_epoch) if not kill_switch else None
    return evaluate_entry_lock(kill_switch=kill_switch, daily_pl_usd=daily_pl,
                               equity_usd=equity_usd, max_daily_loss_pct=max_daily_loss_pct)


class FileKillSwitch:
    """Explicit filesystem kill switch: a configured marker path halts entries."""
    def __init__(self, marker_path: str):
        from pathlib import Path
        self.marker_path = Path(marker_path)

    def is_active(self) -> bool:
        return self.marker_path.exists()


def write_kill_switch(marker_path: str, active: bool) -> None:
    """Create/remove an operator marker atomically; parent directory must exist."""
    import os
    from pathlib import Path
    path = Path(marker_path)
    if active:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("active\\n", encoding="utf-8")
        os.replace(tmp, path)
    else:
        path.unlink(missing_ok=True)


def get_server_day_start(bridge, *, now_epoch: float) -> float | None:
    """Ask bridge for today's server-day boundary; never guess timezone."""
    getter = getattr(bridge, "get_server_day_start", None)
    if not callable(getter):
        return None
    try:
        value = getter(now_epoch)
        if value is None:
            return None
        boundary = float(value)
        if not math.isfinite(boundary) or boundary > now_epoch:
            return None
        return boundary
    except Exception:
        return None


def evaluate_total_drawdown(*, equity_usd: float | None, start_balance_usd: float | None,
                            max_drawdown_pct: float, latched: bool = False) -> RiskLockDecision:
    """Fail-closed total drawdown guard; latched until operator reset."""
    if latched:
        return RiskLockDecision(False, "total drawdown lock latched")
    values = (equity_usd, start_balance_usd, max_drawdown_pct)
    if any(value is None or not math.isfinite(float(value)) for value in values):
        return RiskLockDecision(False, "total drawdown inputs unavailable")
    if float(equity_usd) <= 0 or float(start_balance_usd) <= 0 or float(max_drawdown_pct) <= 0:
        return RiskLockDecision(False, "invalid total drawdown input")
    threshold = float(start_balance_usd) * (1.0 - float(max_drawdown_pct) / 100.0)
    if float(equity_usd) <= threshold:
        return RiskLockDecision(False, "total drawdown threshold reached")
    return RiskLockDecision(True, "within total drawdown threshold")


def consecutive_loss_count(trades, after_trade_id: int = 0) -> int:
    """Count contiguous losing Vilona trades, newest first; break on a win.

    Trades with row id at or below ``after_trade_id`` were already reviewed at
    the last operator reset and are ignored. Without this the same historical
    losses re-trigger the lock on the very next scan, so a reset could never
    take effect until a winning trade happened to close. Row id is used rather
    than closing time because broker deal times are server time, not host UTC.
    """
    count = 0
    for trade in sorted(trades, key=lambda row: float(row.get("closed_at") or 0), reverse=True):
        if int(trade.get("id") or 0) <= after_trade_id:
            continue
        net = sum(float(trade.get(field) or 0.0) for field in ("profit", "commission", "swap"))
        if net < 0:
            count += 1
        elif net > 0:
            break
    return count


def evaluate_consecutive_losses(*, loss_count: int, max_losses: int,
                                latched: bool = False) -> RiskLockDecision:
    """Fail-closed consecutive-loss guard; latched until operator reset."""
    if latched:
        return RiskLockDecision(False, "consecutive loss lock latched")
    if loss_count < 0 or max_losses <= 0:
        return RiskLockDecision(False, "invalid consecutive loss input")
    if loss_count >= max_losses:
        return RiskLockDecision(False, "consecutive loss threshold reached")
    return RiskLockDecision(True, "within consecutive loss threshold")



