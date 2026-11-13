"""Unit tests for market-condition guards (D4 weekend cutoff, open grace, stale-tick)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
import pytest

from core.market_guard import (
    evaluate_tick_freshness,
    evaluate_weekend_guard,
    is_weekend_flatten_window,
)


def _epoch_at(year=2026, month=11, day=13, hour=12, minute=0, second=0):
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return dt.timestamp()


# --- Stale tick checks ---

def test_tick_fresh_when_within_max_age():
    now = 1700000050.0
    tick_time = now - 5.0
    dec = evaluate_tick_freshness(tick_timestamp_s=tick_time, now_s=now, max_age_s=30.0)
    assert dec.allowed is True
    assert "fresh" in dec.reason


def test_tick_rejected_when_stale():
    now = 1700000050.0
    tick_time = now - 45.0
    dec = evaluate_tick_freshness(tick_timestamp_s=tick_time, now_s=now, max_age_s=30.0)
    assert dec.allowed is False
    assert "stale tick" in dec.reason


def test_tick_rejected_when_timestamp_none_or_nonfinite():
    dec = evaluate_tick_freshness(tick_timestamp_s=None, now_s=1700000000.0)
    assert dec.allowed is False
    assert "unavailable" in dec.reason

    dec2 = evaluate_tick_freshness(tick_timestamp_s=float("nan"), now_s=1700000000.0)
    assert dec2.allowed is False
    assert "non-finite" in dec2.reason


def test_tick_rejected_when_far_in_the_future():
    now = 1700000000.0
    dec = evaluate_tick_freshness(tick_timestamp_s=now + 300.0, now_s=now)
    assert dec.allowed is False
    assert "future" in dec.reason


# --- D4 Weekend entry cutoff (Friday 22:00 UTC close, cutoff 120m = 20:00 UTC) ---

def test_weekend_guard_allows_regular_weekday():
    # 2026-11-12 is Thursday, 12:00 UTC
    t = _epoch_at(2026, 11, 12, 12, 0)
    dec = evaluate_weekend_guard(server_epoch_s=t)
    assert dec.allowed is True
    assert "open" in dec.reason


def test_weekend_guard_allows_friday_before_cutoff():
    # 2026-11-13 is Friday, 19:59 UTC (1m before 20:00 cutoff)
    t = _epoch_at(2026, 11, 13, 19, 59)
    dec = evaluate_weekend_guard(server_epoch_s=t, entry_cutoff_min=120)
    assert dec.allowed is True


def test_weekend_guard_blocks_friday_at_cutoff():
    # Friday 20:00 UTC = exactly 120m before 22:00 close
    t = _epoch_at(2026, 11, 13, 20, 0)
    dec = evaluate_weekend_guard(server_epoch_s=t, entry_cutoff_min=120)
    assert dec.allowed is False
    assert "cutoff" in dec.reason


def test_weekend_guard_blocks_saturday():
    # 2026-11-14 is Saturday
    t = _epoch_at(2026, 11, 14, 12, 0)
    dec = evaluate_weekend_guard(server_epoch_s=t)
    assert dec.allowed is False
    assert "Saturday" in dec.reason


def test_weekend_guard_blocks_sunday_pre_open():
    # 2026-11-15 is Sunday, 20:00 UTC (1h before 21:00 open)
    t = _epoch_at(2026, 11, 15, 20, 0)
    dec = evaluate_weekend_guard(server_epoch_s=t)
    assert dec.allowed is False
    assert "pre-open" in dec.reason


def test_weekend_guard_blocks_sunday_during_open_grace():
    # Sunday 21:15 UTC (within 30m grace after 21:00 open)
    t = _epoch_at(2026, 11, 15, 21, 15)
    dec = evaluate_weekend_guard(server_epoch_s=t, open_grace_min=30)
    assert dec.allowed is False
    assert "grace" in dec.reason


def test_weekend_guard_allows_sunday_after_open_grace():
    # Sunday 21:31 UTC (past 30m grace)
    t = _epoch_at(2026, 11, 15, 21, 31)
    dec = evaluate_weekend_guard(server_epoch_s=t, open_grace_min=30)
    assert dec.allowed is True


# --- D4 Weekend flatten window (Friday last 60m before 22:00 UTC = 21:00-22:00) ---

def test_flatten_window_active_on_friday_evening():
    t_in = _epoch_at(2026, 11, 13, 21, 30)
    assert is_weekend_flatten_window(server_epoch_s=t_in, flatten_min=60) is True

    t_before = _epoch_at(2026, 11, 13, 20, 59)
    assert is_weekend_flatten_window(server_epoch_s=t_before, flatten_min=60) is False

    t_after_close = _epoch_at(2026, 11, 13, 22, 1)
    assert is_weekend_flatten_window(server_epoch_s=t_after_close, flatten_min=60) is False

    t_thursday = _epoch_at(2026, 11, 12, 21, 30)
    assert is_weekend_flatten_window(server_epoch_s=t_thursday, flatten_min=60) is False
