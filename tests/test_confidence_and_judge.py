"""Tests for setup confidence scoring and fail-closed judge parsing.

The scanner previously emitted a hardcoded confidence of 0.85 for every
qualifying setup, which made the risk-gate threshold meaningless. These tests
lock in that confidence now responds to setup quality, and that an unparseable
or ambiguous judge response can never approve a trade.
"""

from __future__ import annotations

import pytest

from core.quant_scanner import (
    CONFIDENCE_CEIL,
    CONFIDENCE_FLOOR,
    compute_setup_confidence,
)
from agents.judge import parse_judge


STABLE_ATR = [0.001] * 30


# --- confidence scoring ---------------------------------------------------

def test_degenerate_inputs_floor_at_minimum():
    assert compute_setup_confidence(0, 0, 0, 0, [], True) == CONFIDENCE_FLOOR
    assert compute_setup_confidence(1.0, 1.0, 1.0, 0.0, STABLE_ATR, True) == CONFIDENCE_FLOOR


def test_confidence_stays_within_bounds():
    for ema21, ema50, price, atr in [
        (1.0100, 1.0000, 1.0102, 0.001),
        (1.0001, 1.0000, 1.00015, 0.001),
        (1.0500, 1.0000, 1.0495, 0.001),
    ]:
        c = compute_setup_confidence(ema21, ema50, price, atr, STABLE_ATR, True)
        assert CONFIDENCE_FLOOR <= c <= CONFIDENCE_CEIL


def test_wide_trend_separation_scores_higher_than_flat_trend():
    flat = compute_setup_confidence(1.0002, 1.0000, 1.0002, 0.001, STABLE_ATR, True)
    strong = compute_setup_confidence(1.0040, 1.0000, 1.0041, 0.001, STABLE_ATR, True)
    assert strong > flat


def test_pullback_near_ema_scores_higher_than_chasing_band_edge():
    # Same trend, same volatility: only the distance from EMA21 differs.
    near = compute_setup_confidence(1.0040, 1.0000, 1.0041, 0.001, STABLE_ATR, True)
    chase = compute_setup_confidence(1.0040, 1.0000, 1.0019, 0.001, STABLE_ATR, True)
    assert near > chase


def test_volatility_spike_reduces_score():
    stable = compute_setup_confidence(1.0040, 1.0000, 1.0041, 0.001, STABLE_ATR, True)
    spiking = compute_setup_confidence(
        1.0040, 1.0000, 1.0041, 0.010, STABLE_ATR, True
    )
    assert stable > spiking


def test_not_a_constant():
    a = compute_setup_confidence(1.0002, 1.0000, 1.0002, 0.001, STABLE_ATR, True)
    b = compute_setup_confidence(1.0040, 1.0000, 1.0041, 0.001, STABLE_ATR, True)
    assert a != b
    assert not (a == 0.85 and b == 0.85)


def test_bearish_mirrors_bullish():
    bull = compute_setup_confidence(1.0040, 1.0000, 1.0041, 0.001, STABLE_ATR, True)
    bear = compute_setup_confidence(0.9960, 1.0000, 0.9959, 0.001, STABLE_ATR, False)
    assert abs(bull - bear) < 1e-6


def test_weak_setup_falls_below_default_gate():
    # A near-flat pair chased at the band edge should not clear min_confidence.
    weak = compute_setup_confidence(1.0002, 1.0000, 1.0019, 0.001, STABLE_ATR, True)
    assert weak < 0.55


# --- judge fail-closed ----------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        "{}",
        '{"reason": "looks fine"}',
        '{"verdict": "MAYBE", "reason": "unclear"}',
        '{"verdict": "", "reason": "empty"}',
        '{"verdict": "APPROVED", "reason": "close enough"}',
        '{"verdict": null}',
        "```json\n{}\n```",
    ],
)
def test_judge_vetoes_on_anything_but_explicit_approve(raw):
    approved, reason = parse_judge(raw)
    assert approved is False
    assert isinstance(reason, str)


def test_judge_approves_only_on_explicit_approve():
    approved, reason = parse_judge('{"verdict": "APPROVE", "reason": "clean setup"}')
    assert approved is True
    assert "clean setup" in reason


def test_judge_accepts_fenced_json_with_approve():
    raw = '```json\n{"verdict": "APPROVE", "reason": "ok"}\n```'
    approved, _ = parse_judge(raw)
    assert approved is True


def test_judge_is_case_insensitive_on_approve():
    approved, _ = parse_judge('{"verdict": "approve", "reason": "ok"}')
    assert approved is True
