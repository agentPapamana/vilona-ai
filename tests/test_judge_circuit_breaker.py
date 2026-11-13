"""Circuit breaker prevents repeated slow judge calls after failures."""

import time

from core.judge_circuit_breaker import JudgeCircuitBreaker


def test_opens_after_threshold_and_blocks_calls():
    b = JudgeCircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    assert b.allow() is True
    b.record_failure()
    assert b.allow() is True
    b.record_failure()
    assert b.allow() is False
    assert b.state() == "open"


def test_success_resets_consecutive_failures():
    b = JudgeCircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert b.allow() is True
    assert b.failure_count == 1


def test_cooldown_transitions_open_to_half_open(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    b = JudgeCircuitBreaker(failure_threshold=1, cooldown_seconds=10)
    b.record_failure()
    assert b.allow() is False
    now[0] = 111.0
    assert b.allow() is True
    assert b.state() == "half_open"
    b.record_success()
    assert b.state() == "closed"


def test_half_open_allows_only_one_probe():
    b = JudgeCircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    b.record_failure()
    assert b.allow() is False
    # Simulate cooldown expiry for the single half-open probe.
    b._opened_at -= 61
    assert b.allow() is True
    assert b.allow() is False
    b.record_failure()
    assert b.state() == "open"
