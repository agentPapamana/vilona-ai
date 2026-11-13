"""Order safety gates: magic tagging, post-fill SL verification, daily P/L.

Without these, positions opened by Vilona are indistinguishable from manual
trades (magic=0), and a position can stay open with no stop-loss after the
modify call fails. Both defects were observed live on the demo account.
"""
from __future__ import annotations

import pytest

from trading.bridge_base import Position, TradeResult


class FakeBridge:
    """Records calls; scriptable failure for modify_position_sltp."""

    def __init__(
        self,
        modify_ok: bool = True,
        ticket: int = 12345,
        entry_sl_applied: bool = True,
    ):
        self.orders: list[dict] = []
        self.modifies: list[dict] = []
        self.closes: list[dict] = []
        self._modify_ok = modify_ok
        # Whether the broker honoured the stop attached to the entry order.
        # Some EAs do honour it and then refuse a follow-up modify; the default
        # here is the pessimistic broker that drops the entry stop.
        self._entry_sl_applied = entry_sl_applied
        self._ticket = ticket

    def send_market_order(self, **kwargs) -> TradeResult:
        self.orders.append(kwargs)
        return TradeResult(success=True, message="ok", ticket=self._ticket)

    def modify_position_sltp(self, ticket, symbol, sl=None, tp=None) -> TradeResult:
        self.modifies.append({"ticket": ticket, "symbol": symbol, "sl": sl, "tp": tp})
        if not self._modify_ok:
            return TradeResult(success=False, message="position_modify_failed")
        return TradeResult(success=True, message="SL/TP modified", new_sl=sl, new_tp=tp)

    def close_position(self, ticket, symbol, volume=None, comment="", magic=0) -> TradeResult:
        self.closes.append({"ticket": ticket, "symbol": symbol, "volume": volume})
        return TradeResult(success=True, message="Position closed")

    def get_positions(self, symbol=None):
        if not self.orders:
            return []
        # Model a filled position so post-fill SL verification can be exercised.
        if self.modifies and self._modify_ok:
            sl = float(self.modifies[-1].get("sl") or 0.0)
            tp = float(self.modifies[-1].get("tp") or 0.0)
        elif self._entry_sl_applied:
            sl = float(self.orders[0].get("sl", 0) or 0.0)
            tp = float(self.orders[0].get("tp", 0) or 0.0)
        else:
            sl = 0.0
            tp = 0.0
        return [Position(
            ticket=self._ticket,
            symbol=symbol or self.orders[0]["symbol"],
            type=0,
            volume=self.orders[0]["volume"],
            price_open=4019.92,
            price_current=4020.64,
            sl=sl,
            tp=tp,
            profit=0.0,
            magic=self.orders[0].get("magic", 0) or 0,
            comment="",
        )]

    def _applied_sl(self) -> float:
        """SL the broker actually holds: last successful modify, else from entry."""
        if self.modifies and self._modify_ok:
            return float(self.modifies[-1]["sl"] or 0.0)
        if self.modifies and not self._modify_ok:
            return float(self.orders[0].get("sl") or 0.0)
        return float(self.orders[0].get("sl") or 0.0)


def test_magic_number_sent_with_order():
    from core.order_safety import place_protected_order

    bridge = FakeBridge()
    result = place_protected_order(
        bridge,
        symbol="XAUUSD",
        action="buy",
        volume=0.01,
        sl=4016.48,
        tp=4030.0,
        magic=77001,
    )

    assert result.success is True
    assert bridge.orders[0]["magic"] == 77001


def test_magic_never_zero_when_configured():
    from core.order_safety import resolve_magic

    # Same symbol always maps to the same stable magic, never 0.
    a = resolve_magic("XAUUSD")
    b = resolve_magic("XAUUSD")
    assert a == b
    assert a != 0
    assert resolve_magic("EURUSD") != a


def test_sl_is_verified_after_fill():
    from core.order_safety import place_protected_order

    bridge = FakeBridge(entry_sl_applied=False)
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01, sl=4016.48, magic=77001
    )

    assert result.success is True
    assert len(bridge.modifies) == 1
    assert bridge.modifies[0]["sl"] == 4016.48
    assert result.sl_verified is True


def test_entry_sl_is_verified_without_redundant_modify():
    from core.order_safety import place_protected_order

    bridge = FakeBridge(entry_sl_applied=True, modify_ok=False)
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01, sl=4016.48, magic=77001
    )

    assert result.success is True
    assert result.sl_verified is True
    assert bridge.modifies == [], "do not modify a position already protected at entry"
    assert bridge.closes == [], "do not close a protected position due to modify refusal"


def test_position_is_closed_when_sl_cannot_be_applied():
    from core.order_safety import place_protected_order

    bridge = FakeBridge(entry_sl_applied=False, modify_ok=False)
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01, sl=4016.48, magic=77001
    )

    assert result.success is False
    assert result.sl_verified is False
    assert len(bridge.closes) == 1, "unprotected position must be closed"
    assert bridge.closes[0]["ticket"] == 12345


def test_order_without_sl_is_refused_before_send():
    from core.order_safety import place_protected_order

    bridge = FakeBridge()
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01, sl=None, magic=77001
    )

    assert result.success is False
    assert bridge.orders == [], "an order with no SL must never reach the broker"


def test_daily_pl_computed_from_closed_deals():
    from core.order_safety import compute_daily_pl

    deals = [
        {"ticket": 1, "symbol": "XAUUSD", "profit": 12.5, "time": 1794330000, "entry": 1},
        {"ticket": 2, "symbol": "XAUUSD", "profit": -4.0, "time": 1794335000, "entry": 1},
        {"ticket": 3, "symbol": "EURUSD", "profit": 2.0, "time": 1794300000, "entry": 1},
    ]
    total = compute_daily_pl(deals, since_epoch=1794320000)
    assert total == pytest.approx(8.5)


class SilentBridge(FakeBridge):
    """Reproduces the live defect: modify reports success, broker keeps sl=0.

    The EA returned success for the SL/TP modify, yet the position was found on
    the account with sl=0.0. Trusting the modify reply alone let naked
    positions survive, so verification must read the position back.
    """

    def __init__(self, applied_sl: float = 0.0, ticket: int = 12345):
        super().__init__(ticket=ticket)
        self._silent_sl = applied_sl

    def get_positions(self, symbol=None):
        if not self.modifies:
            # Before the modify there is nothing to report yet.
            return []
        return [Position(
            ticket=self._ticket,
            symbol=symbol or "XAUUSD",
            type=0,
            volume=0.01,
            price_open=4019.92,
            price_current=4020.64,
            sl=self._silent_sl,
            tp=0.0,
            profit=0.0,
            magic=770714,
            comment="",
        )]


def test_modify_success_without_applied_sl_is_treated_as_failure():
    from core.order_safety import place_protected_order

    bridge = SilentBridge(applied_sl=0.0)
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01,
        sl=4016.48, tp=4030.0, magic=770714,
    )

    assert result.success is False, "a position left with sl=0 must not report success"
    assert result.closed_after_failure is True, "naked position must be closed"
    assert [c["ticket"] for c in bridge.closes] == [12345]


def test_modify_success_with_applied_sl_passes():
    from core.order_safety import place_protected_order

    bridge = SilentBridge(applied_sl=4016.48)
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01,
        sl=4016.48, tp=4030.0, magic=770714,
    )

    assert result.success is True
    assert result.sl_verified is True
    assert bridge.closes == [], "protected position must not be closed"


def test_sl_on_wrong_side_of_price_is_refused_before_sending():
    from core.order_safety import place_protected_order

    bridge = FakeBridge()
    # BUY with SL above entry is inverted and would never trigger.
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01,
        sl=4100.0, tp=4300.0, magic=770714,
        reference_price=4019.92,
    )

    assert result.success is False
    assert "stop-loss" in result.message.lower()
    assert bridge.orders == [], "an inverted stop must never reach the broker"


def test_sl_too_close_to_price_is_refused_before_sending():
    from core.order_safety import place_protected_order

    bridge = FakeBridge()
    # 0.5 USD stop on gold is far inside typical broker stops_level.
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01,
        sl=4019.42, tp=4100.0, magic=770714,
        reference_price=4019.92, min_stop_distance=1.0,
    )

    assert result.success is False
    assert bridge.orders == []


def test_sl_is_normalised_to_symbol_digits():
    from core.order_safety import place_protected_order

    bridge = FakeBridge()
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01,
        sl=4013.11456789, tp=4023.11999, magic=770714, digits=2,
    )

    assert result.success is True
    assert bridge.orders[0]["sl"] == 4013.11
    assert bridge.orders[0]["tp"] == 4023.12


def test_min_stop_distance_resolved_per_symbol():
    from core.order_safety import resolve_min_stop_distance

    # Live evidence: a 1.0 USD stop on XAUUSD was rejected by the EA with
    # position_modify_failed, while 5.0 was accepted.
    assert resolve_min_stop_distance("XAUUSD") == pytest.approx(10.0)
    assert resolve_min_stop_distance("XAUUSD.a") == pytest.approx(10.0)
    assert resolve_min_stop_distance("XAGUSD") == pytest.approx(0.5)
    # Unlisted forex pairs fall back to the flat default.
    assert resolve_min_stop_distance("EURUSD") == pytest.approx(0.0)


def test_configured_min_stop_distance_blocked_tight_gold_stop():
    from core.order_safety import place_protected_order, resolve_min_stop_distance

    bridge = FakeBridge()
    min_dist = resolve_min_stop_distance("XAUUSD")
    result = place_protected_order(
        bridge, symbol="XAUUSD", action="buy", volume=0.01,
        sl=4027.8, tp=4040.0, magic=770714,
        reference_price=4028.8, min_stop_distance=min_dist, digits=2,
    )

    assert result.success is False, "1.0 USD stop on gold must be refused locally"
    assert bridge.orders == [], "the rejected stop must never reach the broker"


class _RoundingBridge(FakeBridge):
    """Broker that stores the entry stop rounded to 2 digits (the observed
    EURUSD failure: 1.10575 -> 1.11). A successful modify fixes it only when
    ``modify_fixes`` is True."""

    def __init__(self, modify_fixes: bool):
        super().__init__(modify_ok=True, entry_sl_applied=True)
        self._modify_fixes = modify_fixes

    def get_positions(self, symbol=None):
        positions = super().get_positions(symbol)
        for p in positions:
            if not (self.modifies and self._modify_fixes):
                p.sl = round(float(self.orders[0]["sl"]), 2)
        return positions


def test_broker_stop_far_from_requested_is_corrected_not_accepted():
    from core.order_safety import place_protected_order

    bridge = _RoundingBridge(modify_fixes=True)
    result = place_protected_order(
        bridge, symbol="EURUSD", action="sell", volume=0.01,
        sl=1.10575, tp=1.10418, magic=770892, digits=5,
    )

    assert result.success is True
    assert bridge.modifies, "a wrong broker stop must trigger a corrective modify"
    assert bridge.modifies[0]["sl"] == pytest.approx(1.10575)


def test_broker_stop_that_cannot_be_corrected_closes_the_position():
    from core.order_safety import place_protected_order

    bridge = _RoundingBridge(modify_fixes=False)
    result = place_protected_order(
        bridge, symbol="EURUSD", action="sell", volume=0.01,
        sl=1.10575, tp=1.10418, magic=770892, digits=5,
    )

    assert result.success is False
    assert bridge.closes, "a stop 50 pips from plan must not stay open"
