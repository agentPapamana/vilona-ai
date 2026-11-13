"""Tests for LLM position manager MODIFY validation (SL tightening, TP extension)."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from core.order_safety import MAGIC_BASE
from core.quant_scanner import QuantScanner


class _RecordingBridge:
    def __init__(self, positions, tick, candles, symbol_info):
        self._positions = positions
        self._tick = tick
        self._candles = candles
        self._info = symbol_info
        self.modify_calls = []
        self.close_calls = []

    def get_positions(self, symbol=None):
        return list(self._positions)

    def get_tick(self, symbol):
        return self._tick

    def get_candles(self, symbol, timeframe, limit=30):
        return self._candles

    def get_symbol_info(self, symbol):
        return self._info

    def modify_position_sltp(self, ticket, symbol, sl=None, tp=None):
        self.modify_calls.append({"ticket": ticket, "symbol": symbol, "sl": sl, "tp": tp})
        for p in self._positions:
            if p.ticket == ticket:
                p.sl = sl or p.sl
                p.tp = tp or p.tp
        return SimpleNamespace(success=True, message="modified")

    def close_position(self, ticket, symbol, volume=None, comment="", magic=0):
        self.close_calls.append({"ticket": ticket})
        return SimpleNamespace(success=True, message="closed")


def _candles(count=30, atr_val=1.0):
    rows = []
    for i in range(count):
        c = 100.0 + i * 0.1
        rows.append({"time": 1700000000 + i * 900, "open": c, "high": c + atr_val, "low": c, "close": c + 0.5})
    return rows


def _make_scanner(monkeypatch, llm_decision, position, tick, atr_val=1.0):
    from core import state_db as state_db_mod
    monkeypatch.setattr(state_db_mod, "get_config", lambda: SimpleNamespace(
        database=SimpleNamespace(path=":memory:")
    ))
    candles = _candles(30, atr_val)
    info = SimpleNamespace(digits=2, point=0.01, tick_size=0.01, tick_value=1.0)
    bridge = _RecordingBridge([position], tick, candles, info)
    cfg = SimpleNamespace(
        llm=SimpleNamespace(enabled=True, timeout=10),
        trading=SimpleNamespace(weekend_flatten_min=0),
        paper_trading=False,
    )
    from core import llm_manager as llm_mod
    monkeypatch.setattr(llm_mod, "position_decision", lambda *a, **k: llm_decision)
    scanner = QuantScanner(bridge=bridge, cfg=cfg, dashboard=None)
    scanner.db = None
    return scanner, bridge


def test_tp_extension_allowed_when_sl_already_protects_entry(monkeypatch):
    """BUY: entry=100, cur=103, SL=101 (risk-free). Extending TP from 105 to 108 is accepted."""
    pos = SimpleNamespace(
        ticket=7001, symbol="XAUUSD", type=0, volume=0.01,
        price_open=100.0, price_current=103.0, sl=101.0, tp=105.0, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=103.0, ask=103.1, timestamp_ms=1700000000000)
    decision = {"action": "MODIFY", "reason": "strong trend", "sl_price": 101.5, "tp_price": 108.0}
    scanner, bridge = _make_scanner(monkeypatch, decision, pos, tick, atr_val=2.0)

    scanner.manage_open_positions()

    assert len(bridge.modify_calls) == 1
    call = bridge.modify_calls[0]
    assert call["sl"] == pytest.approx(101.5)
    assert call["tp"] == pytest.approx(108.0)


def test_tp_extension_refused_when_position_not_risk_free(monkeypatch):
    """BUY: entry=100, cur=101, SL=98 (under water), TP=103 (33% progress). Extending TP to 110 is refused; SL tightening is accepted."""
    pos = SimpleNamespace(
        ticket=7002, symbol="XAUUSD", type=0, volume=0.01,
        price_open=100.0, price_current=101.0, sl=98.0, tp=103.0, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=101.0, ask=101.1, timestamp_ms=1700000000000)
    # LLM tightens SL to 99 (still below entry) and tries to extend TP to 110.
    decision = {"action": "MODIFY", "reason": "extend target", "sl_price": 99.0, "tp_price": 110.0}
    scanner, bridge = _make_scanner(monkeypatch, decision, pos, tick, atr_val=1.0)

    scanner.manage_open_positions()

    assert len(bridge.modify_calls) == 1
    call = bridge.modify_calls[0]
    assert call["sl"] == pytest.approx(99.0), "tighter SL must be applied"
    assert call["tp"] == pytest.approx(103.0), "TP extension must be blocked until risk-free"


def test_llm_not_called_below_thirty_percent_progress(monkeypatch):
    """Positions under 30% of the TP distance are left to the deterministic ladder only."""
    pos = SimpleNamespace(
        ticket=7010, symbol="USDJPY", type=0, volume=0.01,
        price_open=100.0, price_current=101.0, sl=98.0, tp=105.0, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=101.0, ask=101.1, timestamp_ms=1700000000000)
    llm_calls = []
    scanner, bridge = _make_scanner(monkeypatch, None, pos, tick, atr_val=1.0)
    from core import llm_manager as llm_mod
    monkeypatch.setattr(llm_mod, "position_decision", lambda *a, **k: llm_calls.append(1))

    scanner.manage_open_positions()

    assert llm_calls == [], "LLM must not review a position at 20% progress"
    # The ladder still runs: 1 ATR in profit -> break-even + 0.1 ATR.
    assert [c["sl"] for c in bridge.modify_calls] == [pytest.approx(100.1)]


def test_tp_extension_capped_at_six_atr_from_entry(monkeypatch):
    """BUY: entry=100, ATR=2. Extending TP beyond 100 + 6*2 = 112 is refused; stays at current TP."""
    pos = SimpleNamespace(
        ticket=7003, symbol="XAUUSD", type=0, volume=0.01,
        price_open=100.0, price_current=104.0, sl=101.0, tp=106.0, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=104.0, ask=104.1, timestamp_ms=1700000000000)
    decision = {"action": "MODIFY", "reason": "moonshot target", "sl_price": 102.0, "tp_price": 120.0}
    scanner, bridge = _make_scanner(monkeypatch, decision, pos, tick, atr_val=2.0)

    scanner.manage_open_positions()

    assert len(bridge.modify_calls) == 1
    call = bridge.modify_calls[0]
    assert call["sl"] == pytest.approx(102.0)
    assert call["tp"] == pytest.approx(106.0), "TP beyond 6 ATR cap must be ignored"


def test_sl_widening_is_always_ignored(monkeypatch):
    """BUY: cur_sl=101. Proposed SL=99 must not be applied."""
    pos = SimpleNamespace(
        ticket=7004, symbol="XAUUSD", type=0, volume=0.01,
        price_open=100.0, price_current=103.0, sl=101.0, tp=105.0, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=103.0, ask=103.1, timestamp_ms=1700000000000)
    decision = {"action": "MODIFY", "reason": "give room", "sl_price": 99.0, "tp_price": 105.0}
    scanner, bridge = _make_scanner(monkeypatch, decision, pos, tick, atr_val=1.0)

    scanner.manage_open_positions()

    assert len(bridge.modify_calls) == 0, "widening SL with unchanged TP must do nothing"

def test_break_even_rounds_sl_to_symbol_digits(monkeypatch):
    """USDJPY has 3 digits: break-even SL must be rounded to 3 digits, not 5."""
    pos = SimpleNamespace(
        ticket=7005, symbol="USDJPY", type=0, volume=0.01,
        price_open=159.887, price_current=161.000, sl=159.700, tp=165.000, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=161.000, ask=161.005, timestamp_ms=1700000000000)
    # LLM disabled/HOLD, so deterministic BE runs
    scanner, bridge = _make_scanner(monkeypatch, {"action": "HOLD", "reason": "holding"}, pos, tick, atr_val=1.0)
    bridge._info = SimpleNamespace(digits=3, point=0.001, tick_size=0.001, tick_value=1.0)

    scanner.manage_open_positions()

    assert len(bridge.modify_calls) == 1
    call = bridge.modify_calls[0]
    # open_px (159.887) + 0.1 * 1.0 = 159.987
    expected_sl = round(159.887 + 0.1 * 1.0, 3)
    assert call["sl"] == expected_sl
    assert len(str(call["sl"]).split(".")[1]) <= 3


def test_profit_lock_ladder_steps_for_buy():
    from core.quant_scanner import profit_lock_sl
    common = dict(is_buy=True, open_px=100.0, tp=110.0, atr=4.0, digits=2)
    # 30% progress, below 1 ATR: nothing to lock yet.
    assert profit_lock_sl(cur_px=103.0, cur_sl=95.0, **common) is None
    # 1 ATR (40% progress): break-even plus 0.1 ATR.
    assert profit_lock_sl(cur_px=104.0, cur_sl=95.0, **common) == pytest.approx(100.4)
    # 50% progress: lock 25% of the TP distance.
    assert profit_lock_sl(cur_px=105.0, cur_sl=100.4, **common) == pytest.approx(102.5)
    # 75% progress: lock 50% of the TP distance.
    assert profit_lock_sl(cur_px=107.5, cur_sl=102.5, **common) == pytest.approx(105.0)


def test_profit_lock_ladder_never_loosens_and_mirrors_for_sell():
    from core.quant_scanner import profit_lock_sl
    # Stop already above the 50%-progress lock: no change.
    assert profit_lock_sl(is_buy=True, open_px=100.0, cur_px=105.0, tp=110.0,
                          cur_sl=103.0, atr=4.0, digits=2) is None
    # SELL at 75% progress: lock 50% of the TP distance below entry.
    assert profit_lock_sl(is_buy=False, open_px=100.0, cur_px=92.5, tp=90.0,
                          cur_sl=104.0, atr=4.0, digits=2) == pytest.approx(95.0)
    # Losing trade: never touched.
    assert profit_lock_sl(is_buy=False, open_px=100.0, cur_px=101.0, tp=90.0,
                          cur_sl=104.0, atr=4.0, digits=2) is None


def test_scan_cycle_invokes_manage_open_positions(monkeypatch):
    """scan_cycle must execute manage_open_positions to protect running trades."""
    pos = SimpleNamespace(
        ticket=7006, symbol="USDJPY", type=0, volume=0.01,
        price_open=159.887, price_current=161.000, sl=159.700, tp=162.000, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=161.000, ask=161.005, timestamp_ms=1700000000000)
    scanner, bridge = _make_scanner(monkeypatch, {"action": "HOLD", "reason": "holding"}, pos, tick, atr_val=1.0)
    bridge._info = SimpleNamespace(digits=3, point=0.001, tick_size=0.001, tick_value=1.0)
    monkeypatch.setattr(scanner, "evaluate_symbol", lambda sym: None)

    scanner.scan_cycle()

    assert len(bridge.modify_calls) == 1, "scan_cycle must trigger manage_open_positions"


def test_llm_position_review_debounced_at_sixty_seconds(monkeypatch):
    """LLM manager must not be called repeatedly within the 60s debounce window."""
    pos = SimpleNamespace(
        ticket=7007, symbol="XAUUSD", type=0, volume=0.01,
        price_open=100.0, price_current=103.0, sl=101.0, tp=105.0, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=103.0, ask=103.1, timestamp_ms=1700000000000)
    llm_calls = []

    def fake_decision(*a, **k):
        llm_calls.append(1)
        return {"action": "MODIFY", "reason": "tighten", "sl_price": 101.5, "tp_price": 105.0}

    scanner, bridge = _make_scanner(monkeypatch, None, pos, tick, atr_val=1.0)
    from core import llm_manager as llm_mod
    monkeypatch.setattr(llm_mod, "position_decision", fake_decision)

    # First review -> LLM called
    scanner.manage_open_positions()
    assert len(llm_calls) == 1
    assert len(bridge.modify_calls) == 1

    # Second review immediately (<60s) -> LLM NOT called
    scanner.manage_open_positions()
    assert len(llm_calls) == 1, "LLM must be debounced within 60 seconds"


def test_llm_called_on_losing_position_to_cut_loss_early(monkeypatch):
    """When loss exceeds -0.8 ATR, LLM is called and can CLOSE to cut loss early."""
    pos = SimpleNamespace(
        ticket=7020, symbol="USDJPY", type=0, volume=0.01,
        price_open=160.000, price_current=159.000, sl=157.000, tp=163.000, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=159.000, ask=159.005, timestamp_ms=1700000000000)
    decision = {"action": "CLOSE", "reason": "trend breakdown"}
    scanner, bridge = _make_scanner(monkeypatch, decision, pos, tick, atr_val=1.0)
    bridge._info = SimpleNamespace(digits=3, point=0.001, tick_size=0.001, tick_value=1.0)

    scanner.manage_open_positions()

    assert len(bridge.close_calls) == 1
    assert bridge.close_calls[0]["ticket"] == 7020


def test_llm_tightens_sl_on_losing_position(monkeypatch):
    """When loss exceeds -0.8 ATR, LLM can MODIFY to tighten SL closer to current price."""
    pos = SimpleNamespace(
        ticket=7021, symbol="USDJPY", type=0, volume=0.01,
        price_open=160.000, price_current=159.000, sl=157.000, tp=163.000, magic=MAGIC_BASE + 1,
    )
    tick = SimpleNamespace(bid=159.000, ask=159.005, timestamp_ms=1700000000000)
    # Move SL from 157.000 to 158.500 (tighter stop, caps loss to 1.5 instead of 3.0)
    decision = {"action": "MODIFY", "reason": "cap loss tighter", "sl_price": 158.500, "tp_price": 163.000}
    scanner, bridge = _make_scanner(monkeypatch, decision, pos, tick, atr_val=1.0)
    bridge._info = SimpleNamespace(digits=3, point=0.001, tick_size=0.001, tick_value=1.0)

    scanner.manage_open_positions()

    assert len(bridge.modify_calls) == 1
    call = bridge.modify_calls[0]
    assert call["ticket"] == 7021
    assert call["sl"] == pytest.approx(158.500)
