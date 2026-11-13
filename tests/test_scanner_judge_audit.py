"""The scanner must persist every judge outcome to judge_decisions.

A paper soak is only auditable if vetoes, approvals and LLM failures all land
in the database with enough context to recompute statistics later.
"""

from __future__ import annotations

import pytest

import core.quant_scanner as scanner_mod
from core.quant_scanner import QuantScanner
from tests.test_paper_mode_guard import RecordingBridge, _cfg


class RecordingDB:
    def __init__(self):
        self.judge = []
        self.orders = []

    def save_judge_decision(self, d):
        self.judge.append(d)

    def save_order(self, o):
        self.orders.append(o)

    def save_trade(self, t):
        pass

    def get_positions(self, symbol=None):
        return []


@pytest.fixture()
def scanner(monkeypatch):
    bridge = RecordingBridge()
    cfg = _cfg(paper=True)
    cfg.llm.enabled = True
    # These tests exercise the pre-D1 deterministic fallback path.
    cfg.llm.require_approve = False
    s = QuantScanner(bridge, cfg)
    s.db = RecordingDB()
    s.dashboard = None
    return s


def _force_manager(monkeypatch, decision):
    import core.llm_manager as manager
    monkeypatch.setattr(manager, "entry_decision", lambda *a, **k: decision)
    import llm.client as client
    monkeypatch.setattr(client, "get_llm_client", lambda: object())


def test_veto_is_persisted(scanner, monkeypatch):
    _force_manager(monkeypatch, {"action": "VETO", "reason": "Vetoed: data inconsistent"})
    scanner.bridge.get_server_day_start = lambda now: 1700000000
    scanner.bridge.get_history_deals = lambda since: []
    scanner.evaluate_symbol("EURUSD")
    assert len(scanner.db.judge) == 1
    rec = scanner.db.judge[0]
    assert rec["verdict"] == "VETO"
    assert rec["symbol"] == "EURUSD"
    assert rec["reason"] == "Vetoed: data inconsistent"
    assert rec["confidence"] > 0
    assert rec["latency_ms"] is not None and rec["latency_ms"] >= 0
    assert scanner.db.orders == []


def test_approve_is_persisted_in_paper_mode(scanner, monkeypatch):
    _force_manager(monkeypatch, {"action": "APPROVE", "reason": "Clean setup"})
    scanner.bridge.get_server_day_start = lambda now: 1700000000
    scanner.bridge.get_history_deals = lambda since: []
    scanner.evaluate_symbol("EURUSD")
    assert len(scanner.db.judge) == 1
    assert scanner.db.judge[0]["verdict"] == "APPROVE"
    assert len(scanner.db.orders) == 1
    assert scanner.db.orders[0]["type"] == "paper"


def test_llm_error_uses_deterministic_fallback(scanner, monkeypatch):
    import core.llm_manager as manager
    monkeypatch.setattr(manager, "entry_decision", lambda *a, **k: None)
    import llm.client as client
    monkeypatch.setattr(client, "get_llm_client", lambda: object())
    scanner.bridge.get_server_day_start = lambda now: 1700000000
    scanner.bridge.get_history_deals = lambda since: []
    scanner.evaluate_symbol("EURUSD")
    assert scanner.db.judge[0]["verdict"] == "ERROR"
    assert len(scanner.db.orders) == 1
    assert scanner.db.orders[0]["type"] == "paper"
