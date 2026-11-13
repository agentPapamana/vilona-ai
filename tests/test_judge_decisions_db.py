"""Judge decision audit trail: every verdict must be persisted, vetoes included.

Without this table a paper soak produces no queryable record of why a trade was
approved or vetoed, so win-rate and veto-rate comparisons stay anecdotal.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from core.state_db import StateDB


@pytest.fixture()
def db(tmp_path):
    path = os.path.join(tmp_path, "test_state.db")
    d = StateDB(path=path)
    yield d
    d.close()


def test_judge_decisions_table_exists(db):
    with db._lock:
        rows = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='judge_decisions'"
        ).fetchall()
    assert [r["name"] for r in rows] == ["judge_decisions"]


def test_save_judge_decision_records_veto(db):
    db.save_judge_decision({
        "symbol": "EURUSD",
        "action": "BUY",
        "confidence": 0.88,
        "risk_approved": True,
        "lot_size": 0.08,
        "sl": 1.09900,
        "tp": 1.10200,
        "verdict": "VETO",
        "reason": "Market data inconsistent",
        "latency_ms": 812,
        "error": None,
    })
    rows = db.get_judge_decisions(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "EURUSD"
    assert row["action"] == "BUY"
    assert row["verdict"] == "VETO"
    assert row["risk_approved"] == 1
    assert row["confidence"] == pytest.approx(0.88)
    assert row["latency_ms"] == 812
    assert row["reason"] == "Market data inconsistent"
    assert row["error"] is None
    assert row["created_at"] > 0


def test_save_judge_decision_records_llm_error_without_verdict(db):
    db.save_judge_decision({
        "symbol": "XAUUSD",
        "action": "SELL",
        "confidence": 0.71,
        "risk_approved": True,
        "verdict": None,
        "reason": "judge timeout after 30s",
        "latency_ms": 30000,
        "error": "TimeoutError",
    })
    rows = db.get_judge_decisions(limit=1)
    assert rows[0]["verdict"] is None
    assert rows[0]["error"] == "TimeoutError"


def test_judge_decisions_are_ordered_newest_first(db):
    for i, verdict in enumerate(["VETO", "VETO", "APPROVE"]):
        db.save_judge_decision({
            "symbol": f"PAIR{i}", "action": "BUY", "confidence": 0.9,
            "risk_approved": True, "verdict": verdict, "reason": f"r{i}",
        })
    rows = db.get_judge_decisions(limit=10)
    assert [r["reason"] for r in rows] == ["r2", "r1", "r0"]


def test_judge_decision_stats_counts_approve_and_veto(db):
    for verdict in ["APPROVE", "VETO", "VETO", "ERROR", None]:
        db.save_judge_decision({
            "symbol": "EURUSD", "action": "BUY", "confidence": 0.9,
            "risk_approved": True, "verdict": verdict, "reason": "x",
        })
    stats = db.get_judge_decision_stats()
    assert stats["total"] == 5
    assert stats["approved"] == 1
    assert stats["vetoed"] == 2
    assert stats["errors"] == 1
    assert stats["no_verdict"] == 1
