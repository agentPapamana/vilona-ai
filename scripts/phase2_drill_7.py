"""Drill 7: a frozen bridge feed must raise exactly one alert, then re-arm.

Simulates the failure mode "bridge alive, ticks dead": the scanner accepts a
fresh tick, then the bridge stops updating and every scan cycle sees the same
timestamp. Verifies:
  * stale-tick gate blocks entries (fail closed),
  * heartbeat alerts on the transition only,
  * recovery clears the latch and the next outage alerts again.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.quant_scanner import QuantScanner


class FrozenBridge:
    """Bridge whose tick timestamp is pinned; open positions stay empty."""

    def __init__(self, tick_epoch_s: float):
        self.tick_epoch_s = tick_epoch_s
        self.order_calls = []
        self.alert_calls = 0

    def get_positions(self, symbol=None):
        return []

    def get_pending_orders(self):
        return []

    def get_tick(self, symbol):
        from types import SimpleNamespace
        return SimpleNamespace(
            symbol=symbol, bid=1.1010, ask=1.1012,
            timestamp_ms=int(self.tick_epoch_s * 1000),
        )

    def get_candles(self, symbol, timeframe, limit=100):
        return []


class AlertSink:
    def __init__(self):
        self.messages = []

    def send_alert(self, text: str):
        self.messages.append(text)


def main():
    bridge = FrozenBridge(time.time())
    sink = AlertSink()
    scanner = QuantScanner(bridge, tg_controller=sink)
    scanner._last_heartbeat_tick_s = time.time()

    # Cycle 1: feed fresh -> healthy, no alert.
    scanner.check_bridge_heartbeat()
    assert sink.messages == [], f"healthy feed must not alert: {sink.messages}"
    print("CYCLE 1 (fresh): alerts=0 RESULT PASS")

    # Feed freezes. Pretend 120s passed without any accepted tick.
    scanner._last_heartbeat_tick_s = time.time() - 120.0
    scanner.check_bridge_heartbeat()
    print(f"CYCLE 2 (frozen): alerts={len(sink.messages)} first={sink.messages[0]!r}")
    assert len(sink.messages) == 1, f"expected exactly one alert, got {sink.messages}"
    assert "stalled" in sink.messages[0]

    # Still frozen next cycle: must NOT alert again.
    scanner._last_heartbeat_tick_s = time.time() - 180.0
    scanner.check_bridge_heartbeat()
    assert len(sink.messages) == 1, f"repeat alert leaked: {sink.messages}"
    print("CYCLE 3 (still frozen): alerts still 1 RESULT PASS")

    # Recovery: a fresh accepted tick clears the latch.
    scanner._last_heartbeat_tick_s = time.time()
    scanner.check_bridge_heartbeat()
    assert scanner._heartbeat_down is False
    print("CYCLE 4 (recovered): latch cleared RESULT PASS")

    # Second outage alerts again.
    scanner._last_heartbeat_tick_s = time.time() - 90.0
    scanner.check_bridge_heartbeat()
    assert len(sink.messages) == 2, f"outage after recovery did not re-alert: {sink.messages}"
    print("CYCLE 5 (second outage): alert re-armed RESULT PASS")

    # Stale tick must never place an order, and must not advance the heartbeat.
    bridge.tick_epoch_s = time.time() - 600.0
    before = scanner._last_heartbeat_tick_s
    scanner.evaluate_symbol("EURUSD")
    assert bridge.order_calls == [], "stale feed placed an order"
    assert scanner._last_heartbeat_tick_s == before, "stale tick advanced the heartbeat"
    print("STALE ENTRY: order_calls=0 heartbeat unchanged RESULT PASS")

    print("\nDRILL 7: PASS")


if __name__ == "__main__":
    main()
