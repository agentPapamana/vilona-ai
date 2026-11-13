"""Judge failures must be distinguishable from substantive vetoes.

A 30s timeout previously reached the database as verdict='VETO' with an empty
reason, which made veto statistics meaningless and diagnosis impossible.
"""

from __future__ import annotations

import asyncio

import pytest

import core.llm_gate as gate
from agents import Signal
from agents.risk_manager import RiskVerdict


class _CfgStub:
    pass


def _signal():
    return Signal(
        symbol="EURUSD", action="BUY", confidence=0.9, reason="trend",
        entry_hint=1.10, source_role="trader",
    )


def _risk():
    return RiskVerdict(
        approved=True, reason="ok", lot_size=0.1, sl_price=1.099,
        tp_price=1.102, rr_ratio=2.0,
    )


class _TimeoutClient:
    def generate_signal(self, system_prompt, user_prompt):
        async def _slow():
            await asyncio.sleep(5)
            return '{"verdict":"APPROVE","reason":"ok"}'
        return _slow()


class _RaisingClient:
    def generate_signal(self, system_prompt, user_prompt):
        raise ConnectionError("upstream 502")


class _EmptyClient:
    def generate_signal(self, system_prompt, user_prompt):
        return ""


def test_timeout_is_reported_as_error_not_veto():
    ok, reason = gate.llm_judge_verdict(
        _TimeoutClient(), _signal(), _risk(), timeout_s=0.05
    )
    assert ok is False
    assert reason.startswith("ERROR:"), reason
    assert "TimeoutError" in reason or "timed out" in reason.lower()
    assert "VETO" not in reason.upper()


def test_exception_is_reported_with_its_message():
    ok, reason = gate.llm_judge_verdict(_RaisingClient(), _signal(), _risk())
    assert ok is False
    assert reason.startswith("ERROR:")
    assert "upstream 502" in reason


def test_empty_response_is_reported_as_error():
    ok, reason = gate.llm_judge_verdict(_EmptyClient(), _signal(), _risk())
    assert ok is False
    assert reason.startswith("ERROR:")
    assert "empty" in reason.lower()


def test_substantive_veto_is_not_labelled_error():
    class _VetoClient:
        def generate_signal(self, system_prompt, user_prompt):
            return '{"verdict":"VETO","reason":"Data inconsistent"}'
    ok, reason = gate.llm_judge_verdict(_VetoClient(), _signal(), _risk())
    assert ok is False
    assert not reason.startswith("ERROR:")
    assert reason == "Data inconsistent"


def test_scanner_manager_error_uses_deterministic_fallback(monkeypatch):
    from core.quant_scanner import QuantScanner
    from tests.test_paper_mode_guard import RecordingBridge, _cfg
    import core.llm_manager as manager_mod
    import llm.client as client_mod

    cfg = _cfg(paper=True)
    cfg.llm.enabled = True
    cfg.llm.require_approve = False

    scanner = QuantScanner(RecordingBridge(), cfg)

    class _DB:
        judge, orders = [], []
        def save_judge_decision(self, d): self.judge.append(d)
        def save_order(self, o): self.orders.append(o)
        def save_trade(self, t): pass
    scanner.db = _DB()
    monkeypatch.setattr(manager_mod, "entry_decision", lambda *a, **k: None)
    monkeypatch.setattr(client_mod, "get_llm_client", lambda: object())
    scanner.bridge.get_server_day_start = lambda now: 1700000000
    scanner.bridge.get_history_deals = lambda since: []
    scanner.evaluate_symbol("EURUSD")

    assert scanner.db.judge[0]["verdict"] == "ERROR"
    assert scanner.db.judge[0]["error"] == "LLMError"
    assert len(scanner.db.orders) == 1
    assert scanner.db.orders[0]["type"] == "paper"
