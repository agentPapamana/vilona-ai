"""Regression tests for Phase 1 code gates:
- B10-1: break-even path magic ownership filter
- B10-4: daily P/L retrieved once per evaluate_symbol call
- D1: require_approve gate on manager ERROR / VETO
- Canary caps: invalid KINGAI_MAX_LOT fails closed, env vars parsed
"""

from __future__ import annotations

import os
from types import SimpleNamespace
import pytest

from config import Config
from core.order_safety import MAGIC_BASE
from core.quant_scanner import QuantScanner
from core.risk_lock import evaluate_entry_lock
from tests.test_paper_mode_guard import RecordingBridge, _cfg


class FakePos:
    def __init__(self, ticket: int, symbol: str, price_open: float, price_current: float,
                 sl: float, tp: float, pos_type: int = 0, magic: int = 0):
        self.ticket = ticket
        self.symbol = symbol
        self.price_open = price_open
        self.price_current = price_current
        self.sl = sl
        self.tp = tp
        self.type = pos_type
        self.magic = magic


class LiveBridge(RecordingBridge):
    """RecordingBridge whose get_positions accepts an optional symbol argument."""

    def __init__(self, open_positions=None):
        super().__init__()
        self._open_positions = open_positions or []

    def get_positions(self, symbol=None):
        if symbol is None:
            return list(self._open_positions)
        return [p for p in self._open_positions if getattr(p, "symbol", None) == symbol]


# ---------------------------------------------------------------------------
# 1. B10-1 Foreign-position modification in break-even path
# ---------------------------------------------------------------------------

def test_break_even_modifies_only_vilona_magic_positions():
    """Deterministic break-even must touch only positions in [MAGIC_BASE, MAGIC_BASE+1000)."""
    bridge = LiveBridge()
    cfg = _cfg(paper=False)
    scanner = QuantScanner(bridge, cfg)

    # atr will be ~0.00018 based on RecordingBridge candles
    # open at 1.0900, current at 1.1000 => gain is > atr, cur_sl (0.0) < open_px
    foreign_pos = FakePos(
        ticket=1001,
        symbol="EURUSD",
        price_open=1.0900,
        price_current=1.1000,
        sl=0.0,
        tp=1.1200,
        pos_type=0,  # BUY
        magic=0,  # Manual / foreign EA
    )
    vilona_pos = FakePos(
        ticket=1002,
        symbol="EURUSD",
        price_open=1.0900,
        price_current=1.1000,
        sl=0.0,
        tp=1.1200,
        pos_type=0,  # BUY
        magic=MAGIC_BASE + 42,  # Owned by Vilona
    )
    foreign_other_magic = FakePos(
        ticket=1003,
        symbol="EURUSD",
        price_open=1.0900,
        price_current=1.1000,
        sl=0.0,
        tp=1.1200,
        pos_type=0,  # BUY
        magic=MAGIC_BASE + 2000,  # Out of range
    )

    bridge._open_positions = [foreign_pos, vilona_pos, foreign_other_magic]

    scanner.manage_open_positions()

    assert len(bridge.modify_calls) == 1
    call = bridge.modify_calls[0]
    ticket = call["a"][0] if call["a"] else call["k"].get("ticket")
    assert ticket == 1002, f"Expected modification of ticket 1002 only, got {ticket}"


# ---------------------------------------------------------------------------
# 2. B10-4 Duplicate P/L retrieval
# ---------------------------------------------------------------------------

def test_evaluate_symbol_fetches_daily_pl_exactly_once(monkeypatch):
    """evaluate_symbol must obtain daily P/L exactly once per cycle."""
    bridge = LiveBridge()
    cfg = _cfg(paper=True)
    scanner = QuantScanner(bridge, cfg)
    scanner.db = None

    pl_calls = {"day_start": 0, "deals": 0}

    def counting_day_start(*a, **k):
        pl_calls["day_start"] += 1
        return 1700000000

    def counting_deals(*a, **k):
        pl_calls["deals"] += 1
        return []

    bridge.get_server_day_start = counting_day_start
    bridge.get_history_deals = counting_deals

    scanner.evaluate_symbol("EURUSD")

    assert pl_calls["day_start"] == 1, f"Expected 1 call to get_server_day_start, got {pl_calls['day_start']}"
    assert pl_calls["deals"] == 1, f"Expected 1 call to get_history_deals, got {pl_calls['deals']}"


# ---------------------------------------------------------------------------
# 3. D1 require_approve gate
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self):
        self.decisions = []
        self.orders = []

    def save_judge_decision(self, record):
        self.decisions.append(record)

    def save_order(self, record):
        self.orders.append(record)
        return True

    def has_order_setup(self, key):
        return False


def test_require_approve_true_blocks_order_on_manager_error(monkeypatch):
    """When require_approve=True, manager error records blocked approval and sends NO order."""
    import core.llm_manager as manager
    import llm.client as client

    monkeypatch.setattr(client, "get_llm_client", lambda: object())
    monkeypatch.setattr(manager, "entry_decision", lambda *a, **k: None)

    bridge = LiveBridge()
    cfg = _cfg(paper=False)
    cfg.llm.enabled = True
    # No explicit override: Config() must default to require_approve=True.
    assert cfg.llm.require_approve is True

    scanner = QuantScanner(bridge, cfg)
    scanner.db = FakeDB()

    scanner.evaluate_symbol("EURUSD")

    assert bridge.order_calls == [], "Order must NOT be submitted when require_approve=True and manager errors"
    assert len(scanner.db.decisions) == 2
    # First is the LLMError recorded during entry_decision failure
    assert scanner.db.decisions[0]["verdict"] == "ERROR"
    # Second is the fail-closed ApprovalRequired decision
    assert scanner.db.decisions[1]["verdict"] == "ERROR"
    assert scanner.db.decisions[1]["reason"] == "blocked: approval required"


def test_require_approve_false_allows_order_on_manager_error(monkeypatch):
    """When require_approve=False, manager error falls back to deterministic R:R and submits."""
    import core.llm_manager as manager
    import llm.client as client

    monkeypatch.setattr(client, "get_llm_client", lambda: object())
    monkeypatch.setattr(manager, "entry_decision", lambda *a, **k: None)

    bridge = LiveBridge()
    cfg = _cfg(paper=False)
    cfg.llm.enabled = True
    cfg.llm.require_approve = False

    # Simulate filled position returned so order_safety verify_stop passes
    def send_order_mock(*a, **k):
        bridge.order_calls.append({"a": a, "k": k})
        # After fill, order_safety checks bridge.get_positions(symbol)
        pos = FakePos(
            ticket=777,
            symbol="EURUSD",
            price_open=1.10055,
            price_current=1.10055,
            sl=k.get("sl", 1.09),
            tp=k.get("tp", 1.12),
            magic=k.get("magic", MAGIC_BASE),
        )
        bridge._open_positions.append(pos)
        return SimpleNamespace(success=True, message="ok", ticket=777)

    bridge.send_market_order = send_order_mock

    scanner = QuantScanner(bridge, cfg)
    scanner.db = FakeDB()

    scanner.evaluate_symbol("EURUSD")

    assert len(bridge.order_calls) == 1, "Order SHOULD be submitted when require_approve=False (fallback to R:R)"
    assert any(d["verdict"] == "ERROR" for d in scanner.db.decisions)
    assert not any(d.get("reason") == "blocked: approval required" for d in scanner.db.decisions)


def test_manager_veto_blocks_entries_in_both_require_approve_modes(monkeypatch):
    """Substantive VETO from manager must block entry regardless of require_approve flag."""
    import core.llm_manager as manager
    import llm.client as client

    monkeypatch.setattr(client, "get_llm_client", lambda: object())
    monkeypatch.setattr(manager, "entry_decision", lambda *a, **k: {"action": "VETO", "reason": "High news risk"})

    for req in (True, False):
        bridge = LiveBridge()
        cfg = _cfg(paper=False)
        cfg.llm.enabled = True
        cfg.llm.require_approve = req

        scanner = QuantScanner(bridge, cfg)
        scanner.db = FakeDB()

        scanner.evaluate_symbol("EURUSD")

        assert bridge.order_calls == [], f"VETO must block when require_approve={req}"
        assert scanner.db.decisions[-1]["verdict"] == "VETO"
        assert scanner.db.decisions[-1]["reason"] == "High news risk"


# ---------------------------------------------------------------------------
# 4. Canary exposure caps validation
# ---------------------------------------------------------------------------

def test_invalid_max_lot_fails_closed(monkeypatch):
    """Non-numeric or non-positive KINGAI_MAX_LOT must raise, not fall back silently."""
    monkeypatch.setenv("KINGAI_MAX_LOT", "not_a_number")
    with pytest.raises(ValueError):
        Config.from_env()

    monkeypatch.setenv("KINGAI_MAX_LOT", "-5.0")
    with pytest.raises(ValueError, match="KINGAI_MAX_LOT must be positive"):
        Config.from_env()

    monkeypatch.setenv("KINGAI_MAX_LOT", "0")
    with pytest.raises(ValueError, match="KINGAI_MAX_LOT must be positive"):
        Config.from_env()


def test_invalid_max_concurrent_positions_fails_closed(monkeypatch):
    """Non-numeric or non-positive KINGAI_MAX_CONCURRENT_POSITIONS must fail closed."""
    monkeypatch.setenv("KINGAI_MAX_CONCURRENT_POSITIONS", "invalid")
    with pytest.raises(ValueError):
        Config.from_env()

    monkeypatch.setenv("KINGAI_MAX_CONCURRENT_POSITIONS", "0")
    with pytest.raises(ValueError, match="KINGAI_MAX_CONCURRENT_POSITIONS must be positive"):
        Config.from_env()


def test_valid_canary_caps_parsed_from_env(monkeypatch):
    """Valid env vars should populate config fields accurately."""
    monkeypatch.setenv("KINGAI_MAX_LOT", "2.5")
    monkeypatch.setenv("KINGAI_MAX_CONCURRENT_POSITIONS", "8")
    monkeypatch.setenv("KINGAI_LLM_REQUIRE_APPROVE", "false")

    cfg = Config.from_env()
    assert cfg.trading.max_lot == 2.5
    assert cfg.trading.max_concurrent_positions == 8
    assert cfg.llm.require_approve is False


def test_closed_vilona_trade_persisted_idempotently_from_history(tmp_path):
    """A disappeared owned ticket uses broker history and is not duplicated."""
    bridge = LiveBridge([])
    bridge.get_history_deals = lambda since: [{
        "position_id": 7001, "symbol": "EURUSD", "profit": 12.5,
        "commission": -0.3, "swap": -0.1, "price": 1.105, "time": 1700000100,
    }]
    scanner = QuantScanner(bridge, _cfg(paper=True))
    from core.state_db import StateDB
    scanner.db = StateDB(str(tmp_path / "state.db"))
    scanner._known_tickets.add(7001)
    scanner._tracked_positions[7001] = {
        "symbol": "EURUSD", "side": "buy", "quantity": 0.2,
        "entry_price": 1.1, "sl": 1.09, "tp": 1.12,
    }
    scanner.persist_closed_trades()
    scanner.persist_closed_trades()
    rows = scanner.db.get_trades()
    assert len(rows) == 1
    assert rows[0]["ticket"] == 7001
    assert rows[0]["profit"] == 12.5
    scanner.db.close()



def _persist_fake_closed_deal(tmp_path, deal, position_ticket=7001):
    from core.state_db import StateDB
    bridge = LiveBridge([])
    bridge.get_history_deals = lambda since: [deal]
    scanner = QuantScanner(bridge, _cfg(paper=True))
    scanner.db = StateDB(str(tmp_path / "state.db"))
    scanner._known_tickets.add(position_ticket)
    scanner._tracked_positions[position_ticket] = {
        "symbol": "EURUSD", "side": "buy", "quantity": 0.2,
        "entry_price": 1.1, "sl": 1.09, "tp": 1.12,
    }
    scanner.persist_closed_trades()
    rows = scanner.db.get_trades()
    scanner.db.close()
    return rows


def test_closed_trade_uses_position_id_and_deal_fields(tmp_path):
    from core.order_safety import MAGIC_BASE
    rows = _persist_fake_closed_deal(tmp_path, {
        "ticket": 90001, "position_id": 7001, "symbol": "EURUSD",
        "magic": MAGIC_BASE + 1, "profit": 12.5, "commission": -0.3,
        "swap": -0.1, "price": 1.105, "time": 1700000100,
    })
    assert len(rows) == 1
    assert rows[0]["profit"] == 12.5
    assert rows[0]["exit_price"] == 1.105


def test_close_deal_ticket_cannot_override_disagreeing_position_id(tmp_path):
    from core.order_safety import MAGIC_BASE
    rows = _persist_fake_closed_deal(tmp_path, {
        "ticket": 7001, "position_id": 8888, "symbol": "EURUSD",
        "magic": MAGIC_BASE + 1, "profit": 12.5, "commission": -0.3,
        "swap": -0.1, "price": 1.105, "time": 1700000100,
    })
    assert rows == []


def test_closed_trade_rejects_foreign_deal_magic(tmp_path):
    from core.order_safety import MAGIC_BASE
    rows = _persist_fake_closed_deal(tmp_path, {
        "ticket": 90001, "position_id": 7001, "symbol": "EURUSD",
        "magic": MAGIC_BASE + 1000, "profit": 12.5, "commission": -0.3,
        "swap": -0.1, "price": 1.105, "time": 1700000100,
    })
    assert rows == []


def test_closed_trade_legacy_ticket_fallback_without_position_id(tmp_path):
    from core.order_safety import MAGIC_BASE
    rows = _persist_fake_closed_deal(tmp_path, {
        "ticket": 7001, "symbol": "EURUSD", "magic": MAGIC_BASE + 1,
        "profit": 12.5, "commission": -0.3, "swap": -0.1,
        "price": 1.105, "time": 1700000100,
    })
    assert len(rows) == 1
    assert rows[0]["ticket"] == 7001

def test_position_decision_audit_records_modify_and_readback_failure(tmp_path):
    """Position-manager audit method stores requested values and failure result."""
    from core.state_db import StateDB
    db = StateDB(str(tmp_path / "state.db"))
    db.save_position_decision({
        "ticket": 8001, "symbol": "EURUSD", "action": "MODIFY",
        "old_sl": 1.09, "old_tp": 1.12, "requested_sl": 1.1,
        "requested_tp": 1.12, "result": "READBACK_FAILED", "latency_ms": 4,
        "error": "MODIFY readback mismatch",
    })
    rows = db.get_position_decisions(8001)
    assert rows[0]["action"] == "MODIFY"
    assert rows[0]["result"] == "READBACK_FAILED"
    assert rows[0]["requested_sl"] == 1.1
    db.close()


def _make_scanner_with_db(tmp_path, bridge):
    from core.state_db import StateDB
    scanner = QuantScanner(bridge, _cfg(paper=False))
    scanner.db = StateDB(str(tmp_path / "state.db"))
    return scanner
def test_verify_position_action_close_success_when_ticket_absent(tmp_path):
    bridge = LiveBridge(open_positions=[])
    scanner = _make_scanner_with_db(tmp_path, bridge)
    pos = FakePos(ticket=8001, symbol="EURUSD", price_open=1.1, price_current=1.1, sl=1.09, tp=1.12)
    assert scanner._verify_position_action(pos, "CLOSE", None, None) is True
    assert scanner.db.get_position_decisions(8001) == []
    scanner.db.close()


def test_verify_position_action_close_fails_when_ticket_still_open(tmp_path):
    pos = FakePos(ticket=8001, symbol="EURUSD", price_open=1.1, price_current=1.1, sl=1.09, tp=1.12)
    bridge = LiveBridge(open_positions=[pos])
    scanner = _make_scanner_with_db(tmp_path, bridge)
    assert scanner._verify_position_action(pos, "CLOSE", None, None) is False
    decisions = scanner.db.get_position_decisions(8001)
    assert len(decisions) == 1
    assert decisions[0]["result"] == "READBACK_FAILED"
    assert "CLOSE readback mismatch" in decisions[0]["error"]
    scanner.db.close()


def test_verify_position_action_modify_success_when_sl_and_tp_match(tmp_path):
    pos_before = FakePos(ticket=8002, symbol="EURUSD", price_open=1.1, price_current=1.11, sl=1.09, tp=1.12)
    pos_after = FakePos(ticket=8002, symbol="EURUSD", price_open=1.1, price_current=1.11, sl=1.1001, tp=1.13)
    bridge = LiveBridge(open_positions=[pos_after])
    scanner = _make_scanner_with_db(tmp_path, bridge)
    assert scanner._verify_position_action(pos_before, "MODIFY", 1.1001, 1.13) is True
    assert scanner.db.get_position_decisions(8002) == []
    scanner.db.close()


def test_verify_position_action_modify_fails_when_sl_mismatches(tmp_path):
    pos_before = FakePos(ticket=8003, symbol="EURUSD", price_open=1.1, price_current=1.11, sl=1.09, tp=1.12)
    pos_after = FakePos(ticket=8003, symbol="EURUSD", price_open=1.1, price_current=1.11, sl=1.09, tp=1.12)
    bridge = LiveBridge(open_positions=[pos_after])
    scanner = _make_scanner_with_db(tmp_path, bridge)
    assert scanner._verify_position_action(pos_before, "MODIFY", 1.105, 1.12) is False
    decisions = scanner.db.get_position_decisions(8003)
    assert len(decisions) == 1
    assert decisions[0]["result"] == "READBACK_FAILED"
    scanner.db.close()


def test_verify_position_action_records_readback_exception(tmp_path):
    class BrokenBridge(RecordingBridge):
        def get_positions(self, symbol=None):
            raise ConnectionError("bridge disconnected")

    scanner = _make_scanner_with_db(tmp_path, BrokenBridge())
    pos = FakePos(ticket=8004, symbol="EURUSD", price_open=1.1, price_current=1.11, sl=1.09, tp=1.12)
    assert scanner._verify_position_action(pos, "CLOSE", None, None) is False
    decisions = scanner.db.get_position_decisions(8004)
    assert len(decisions) == 1
    assert decisions[0]["result"] == "READBACK_FAILED"
    assert "ConnectionError" in decisions[0]["error"]
    scanner.db.close()
