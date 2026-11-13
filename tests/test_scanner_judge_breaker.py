"""Scanner must stop calling an unhealthy judge and degrade to the R:R gate."""

from __future__ import annotations

import pytest

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


def _scanner(monkeypatch, **breaker):
    cfg = _cfg(paper=True)
    cfg.llm.enabled = True
    # Circuit-breaker degradation is the pre-D1 fallback path.
    cfg.llm.require_approve = False
    cfg.judge_breaker.failure_threshold = breaker.get("failure_threshold", 3)
    cfg.judge_breaker.cooldown_seconds = breaker.get("cooldown_seconds", 120.0)
    s = QuantScanner(RecordingBridge(), cfg)
    s.db = RecordingDB()
    return s


def test_circuit_opens_after_threshold_errors_and_stops_calling_the_judge(monkeypatch):
    import core.llm_manager as manager
    import llm.client as client

    calls = {"n": 0}

    def failing(*a, **k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(manager, "entry_decision", failing)
    monkeypatch.setattr(client, "get_llm_client", lambda: object())

    s = _scanner(monkeypatch, failure_threshold=3, cooldown_seconds=120)
    s.bridge.get_server_day_start = lambda now: 1700000000
    s.bridge.get_history_deals = lambda since: []

    # Three failures trip the breaker.
    for _ in range(3):
        s.evaluate_symbol("EURUSD")
    assert calls["n"] == 3
    assert s.judge_breaker.state() == "open"
    assert [r["verdict"] for r in s.db.judge] == ["ERROR"] * 3

    # Fourth cycle must not call the judge at all.
    s.evaluate_symbol("EURUSD")
    assert calls["n"] == 3, "judge was called while the circuit was open"
    assert s.db.judge[-1]["error"] == "CircuitOpenError"
    # LLM is advisory when unavailable: deterministic R:R decides. PAPER mode
    # records the simulated order but must never call the execution bridge.
    assert len(s.db.orders) == 4
    assert all(order["type"] == "paper" for order in s.db.orders)
    assert s.bridge.order_calls == []


def test_successful_judge_closes_the_circuit(monkeypatch):
    import core.llm_manager as manager
    import llm.client as client
    monkeypatch.setattr(client, "get_llm_client", lambda: object())
    s = _scanner(monkeypatch, failure_threshold=1, cooldown_seconds=120)
    s.bridge.get_server_day_start = lambda now: 1700000000
    s.bridge.get_history_deals = lambda since: []
    monkeypatch.setattr(manager, "entry_decision", lambda *a, **k: None)
    s.evaluate_symbol("EURUSD")
    assert s.judge_breaker.state() == "open"
    s.judge_breaker._opened_at -= 121
    monkeypatch.setattr(manager, "entry_decision", lambda *a, **k: {"action": "APPROVE", "reason": "Clean setup"})
    s.evaluate_symbol("EURUSD")
    assert s.judge_breaker.state() == "closed"
    assert s.db.judge[-1]["verdict"] == "APPROVE"
