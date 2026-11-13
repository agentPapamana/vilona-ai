"""Per-symbol judge metrics: error rate and latency must be queryable.

Recommendation 3 of the audit: without per-symbol error rate and latency the
soak cannot show whether failures cluster on one instrument or one hour.
"""

from __future__ import annotations

import os

import pytest

from core.state_db import StateDB


@pytest.fixture()
def db(tmp_path):
    d = StateDB(path=os.path.join(tmp_path, "m.db"))
    yield d
    d.close()


def _row(db, symbol, verdict, latency):
    db.save_judge_decision({
        "symbol": symbol, "action": "BUY", "confidence": 0.9,
        "risk_approved": True, "verdict": verdict, "reason": "x",
        "latency_ms": latency, "error": "TimeoutError" if verdict == "ERROR" else None,
    })


def test_metrics_reports_error_rate_and_latency_per_symbol(db):
    _row(db, "EURUSD", "APPROVE", 900)
    _row(db, "EURUSD", "ERROR", 8000)
    _row(db, "EURUSD", "VETO", 1100)
    _row(db, "XAUUSD", "ERROR", 8000)
    _row(db, "XAUUSD", "ERROR", 8000)

    metrics = db.get_judge_decision_metrics()

    eur = metrics["EURUSD"]
    assert eur["total"] == 3
    assert eur["errors"] == 1
    assert eur["error_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert eur["avg_latency_ms"] == pytest.approx((900 + 8000 + 1100) / 3, abs=1)

    xau = metrics["XAUUSD"]
    assert xau["total"] == 2
    assert xau["error_rate"] == 1.0
    # A symbol whose judge never answers at all is the important signal.
    assert xau["approved"] == 0


def test_metrics_empty_table_returns_empty_mapping(db):
    assert db.get_judge_decision_metrics() == {}


def test_overall_metrics_count_everything(db):
    _row(db, "EURUSD", "APPROVE", 500)
    _row(db, "XAUUSD", "ERROR", 8000)
    overall = db.get_judge_decision_stats()
    assert overall["total"] == 2
    assert overall["approved"] == 1
    assert overall["errors"] == 1
