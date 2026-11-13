"""Trader LLM wiring: LLM only speaks when the deterministic rule says WAIT.

Contract under test:
  * deterministic BUY/SELL is never overridden by the LLM
  * LLM is consulted only when the rule yields WAIT
  * a mute/failing LLM leaves the decision at WAIT (no order)
  * a parseable LLM BUY becomes a signal, never a direct execution
"""
from __future__ import annotations

import pytest

from agents import Signal
from agents.trader import parse_signal


def test_parse_signal_accepts_buy():
    s = parse_signal('{"action": "BUY", "confidence": 0.7, "reason": "trend"}', "EURUSD")
    assert s.action == "BUY"
    assert s.confidence == 0.7
    assert s.source_role == "trader"


def test_parse_signal_rejects_unknown_action_as_wait():
    s = parse_signal('{"action": "SHORT", "confidence": 0.9}', "EURUSD")
    assert s.action == "WAIT"


def test_parse_signal_garbage_is_wait():
    assert parse_signal("not json at all", "EURUSD").action == "WAIT"
    assert parse_signal("", "EURUSD").action == "WAIT"


def test_parse_signal_extracts_json_from_prose():
    s = parse_signal('Sure: {"action": "SELL", "confidence": 0.6} hope that helps', "XAUUSD")
    assert s.action == "SELL"
    assert s.symbol == "XAUUSD"


# ----------------------------------------------------------- LLM trader gate


def test_trader_llm_skipped_when_rule_already_has_signal():
    from core.llm_trader import llm_trader_signal

    calls = []

    def fake_llm(system_prompt, user_prompt):
        calls.append(user_prompt)
        return '{"action": "SELL", "confidence": 0.9}'

    # Rule produced BUY; LLM must not be consulted or allowed to flip it.
    out = llm_trader_signal(fake_llm, snapshot=None, symbol="EURUSD", rule_action="BUY")
    assert out is None
    assert calls == []


def test_trader_llm_consulted_only_on_wait():
    from core.llm_trader import llm_trader_signal

    class Snap:
        symbol = "EURUSD"
        tick_bid = 1.1
        tick_ask = 1.1002
        spread_pct = 0.0018
        atr_15m = 0.001
        candles_15m = []
        candles_1h = []

    out = llm_trader_signal(
        lambda s, u: '{"action": "BUY", "confidence": 0.66, "reason": "llm"}',
        snapshot=Snap(),
        symbol="EURUSD",
        rule_action="WAIT",
    )
    assert isinstance(out, Signal)
    assert out.action == "BUY"
    assert out.source_role == "trader"


@pytest.mark.parametrize("llm", [
    lambda s, u: None,
    lambda s, u: "",
    lambda s, u: "no idea",
    lambda s, u: (_ for _ in ()).throw(TimeoutError("slow")),
])
def test_trader_llm_failure_leaves_wait(llm):
    from core.llm_trader import llm_trader_signal

    out = llm_trader_signal(llm, snapshot=None, symbol="EURUSD", rule_action="WAIT")
    assert out is None


def test_trader_llm_disabled_never_calls():
    from core.llm_trader import llm_trader_signal

    calls = []
    out = llm_trader_signal(
        lambda s, u: calls.append(1) or '{"action": "BUY"}',
        snapshot=None,
        symbol="EURUSD",
        rule_action="WAIT",
        enabled=False,
    )
    assert out is None
    assert calls == []
