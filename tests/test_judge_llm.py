"""Judge LLM gate: fail-closed veto, never bypasses the risk manager.

Contract under test:
  * a muted / failing LLM must never approve a trade
  * a garbage or ambiguous response must never approve a trade
  * only an explicit APPROVE lets the trade continue
  * judge runs strictly AFTER risk approval, and its veto blocks execution
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import pytest

from agents import MarketSnapshot, Signal
from agents.judge import build_user_prompt, parse_judge
from agents.risk_manager import RiskVerdict


@dataclass
class FakeJudge:
    """Stands in for an LLM trader/judge model with scripted behaviour."""

    response: Optional[str] = None
    raises: Optional[BaseException] = None
    calls: int = 0

    def __call__(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.response


def make_signal() -> Signal:
    return Signal(
        symbol="EURUSD",
        action="BUY",
        confidence=0.8,
        reason="trend",
        entry_hint=1.1012,
        source_role="trader",
    )


def make_risk() -> RiskVerdict:
    return RiskVerdict(
        approved=True,
        lot_size=0.01,
        sl_price=1.0985,
        tp_price=1.1062,
        rr_ratio=2.0,
        reason="ok",
    )


def make_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="EURUSD",
        tick_bid=1.1010,
        tick_ask=1.1012,
        candles_15m=[],
        candles_1h=[],
        spread_pct=0.0002,
        atr_15m=0.0009,
        news_headlines=[],
    )


# ---------------------------------------------------------------- parse_judge


@pytest.mark.parametrize("raw", ["", "garbage", "{}", '{"reason": "looks fine"}', "null"])
def test_parse_judge_fails_closed_on_unusable_payload(raw):
    approved, reason = parse_judge(raw)
    assert approved is False
    assert isinstance(reason, str) and reason


@pytest.mark.parametrize("raw", ['{"verdict": "APPROVE"}', 'Here you go: {"verdict": "APPROVE", "reason": "clean"}'])
def test_parse_judge_allows_explicit_approve(raw):
    approved, reason = parse_judge(raw)
    assert approved is True


def test_parse_judge_rejects_veto_and_unknown_verdict():
    assert parse_judge('{"verdict": "VETO", "reason": "RR thin"}')[0] is False
    # Anything not explicitly APPROVE is not an approval.
    assert parse_judge('{"verdict": "MAYBE"}')[0] is False


# ------------------------------------------------------------- prompt payload


def test_judge_prompt_carries_signal_and_risk_numbers():
    import json

    payload = json.loads(build_user_prompt(make_signal(), make_risk()))
    assert payload["signal"]["symbol"] == "EURUSD"
    assert payload["signal"]["action"] == "BUY"
    assert payload["risk_verdict"]["rr_ratio"] == 2.0
    assert payload["risk_verdict"]["sl_price"] == 1.0985


# ------------------------------------------------------------------ LLM gate


def test_judge_gate_approves_only_on_explicit_approve():
    from core.llm_gate import llm_judge_verdict

    approve = FakeJudge(response='{"verdict": "APPROVE", "reason": "clean setup"}')
    ok, reason = llm_judge_verdict(approve, make_signal(), make_risk())
    assert ok is True
    assert approve.calls == 1

    veto = FakeJudge(response='{"verdict": "VETO", "reason": "chasing"}')
    ok, reason = llm_judge_verdict(veto, make_signal(), make_risk())
    assert ok is False
    assert "chasing" in reason


@pytest.mark.parametrize("judge", [
    FakeJudge(response=None),
    FakeJudge(response=""),
    FakeJudge(response="I cannot answer"),
    FakeJudge(raises=TimeoutError("slow model")),
    FakeJudge(raises=RuntimeError("boom")),
])
def test_judge_gate_fails_closed_on_mute_or_error(judge):
    from core.llm_gate import llm_judge_verdict

    ok, reason = llm_judge_verdict(judge, make_signal(), make_risk())
    assert ok is False
    assert isinstance(reason, str) and reason


def test_judge_gate_is_skipped_when_llm_disabled():
    from core.llm_gate import llm_judge_verdict

    judge = FakeJudge(response='{"verdict": "VETO"}')
    ok, reason = llm_judge_verdict(judge, make_signal(), make_risk(), enabled=False)
    assert ok is True
    assert judge.calls == 0
