"""Bridge heartbeat monitor: detect a frozen MT5/bridge link and alert once per outage.

Motivation
----------
A bridge process that is alive but no longer receiving ticks presents stale
quotes indefinitely. The stale-tick gate stops entries, but the operator needs
to know the feed died rather than silently seeing "no setups".

Contract
--------
``evaluate_bridge_heartbeat`` is pure and returns a decision; the caller owns
clock, alerting and throttling. Recovery re-arms the alert so a second outage
alerts again.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HeartbeatDecision:
    healthy: bool
    reason: str
    # True exactly on the transition into an outage: caller sends one alert here.
    should_alert: bool


def evaluate_bridge_heartbeat(*, last_tick_epoch_s: float | None,
                              now_epoch_s: float | None,
                              threshold_s: float = 60.0,
                              previously_down: bool = False) -> HeartbeatDecision:
    """Fail closed: unknown freshness is an outage.

    ``previously_down`` suppresses repeat alerts; the first healthy reading
    clears it so the next outage alerts again.
    """
    if now_epoch_s is None or last_tick_epoch_s is None:
        return HeartbeatDecision(False, "bridge heartbeat unknown (no tick observed)", not previously_down)
    try:
        last = float(last_tick_epoch_s)
        now = float(now_epoch_s)
        limit = float(threshold_s)
    except (TypeError, ValueError):
        return HeartbeatDecision(False, "bridge heartbeat invalid timestamp", not previously_down)
    if last <= 0 or now <= 0 or limit <= 0:
        return HeartbeatDecision(False, "bridge heartbeat invalid parameters", not previously_down)
    age = now - last
    if age > limit:
        return HeartbeatDecision(
            False, f"bridge feed stalled ({age:.0f}s since last tick > {limit:.0f}s)", not previously_down
        )
    return HeartbeatDecision(True, f"bridge heartbeat ok ({age:.0f}s since last tick)", False)
