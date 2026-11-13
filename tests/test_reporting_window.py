from core.reporting import filter_judge_rows, summarize_judge_rows


def test_get_judge_decisions_since_excludes_older_rows(tmp_path):
    from core.state_db import StateDB

    db = StateDB(str(tmp_path / "window.db"))
    try:
        db.save_judge_decision({
            "symbol": "XAUUSD", "action": "BUY", "confidence": 0.6, "risk_approved": 1,
            "lot_size": 0.01, "sl": 1.0, "tp": 2.0, "verdict": "VETO", "reason": "old",
            "latency_ms": 100, "error": None, "created_at": 100.0,
        })
        db.save_judge_decision({
            "symbol": "XAUUSD", "action": "BUY", "confidence": 0.6, "risk_approved": 1,
            "lot_size": 0.01, "sl": 1.0, "tp": 2.0, "verdict": "VETO", "reason": "new",
            "latency_ms": 100, "error": None, "created_at": 300.0,
        })
        rows_all = db.get_judge_decisions(limit=1000)
        rows_recent = db.get_judge_decisions(limit=1000, since=200.0)
    finally:
        db.close()

    assert [r["reason"] for r in rows_all] == ["new", "old"]
    assert [r["reason"] for r in rows_recent] == ["new"]


def test_filter_judge_rows_preserves_cumulative_by_default():
    rows = [{"created_at": 10}, {"created_at": 20}]
    assert filter_judge_rows(rows, None) == rows


def test_filter_judge_rows_excludes_old_rows_and_includes_cutoff():
    rows = [{"created_at": 9}, {"created_at": 10}, {"created_at": 12}]
    assert filter_judge_rows(rows, 10) == rows[1:]


def test_summary_counts_verdicts_by_symbol_and_latency():
    rows = [
        {"symbol": "XAUUSD", "verdict": "APPROVE", "latency_ms": 100},
        {"symbol": "XAUUSD", "verdict": "ERROR", "latency_ms": 300},
        {"symbol": "EURUSD", "verdict": "VETO", "latency_ms": 200},
        {"symbol": "EURUSD", "verdict": None, "latency_ms": None},
    ]
    stats, metrics = summarize_judge_rows(rows)
    assert stats == {"total": 4, "approved": 1, "vetoed": 1, "errors": 1, "no_verdict": 1}
    assert metrics["XAUUSD"] == {
        "total": 2, "approved": 1, "vetoed": 0, "errors": 1,
        "error_rate": 0.5, "avg_latency_ms": 200.0,
    }
    assert metrics["EURUSD"]["avg_latency_ms"] == 200.0

def test_soak_setup_identity_uses_durable_key_before_legacy_trade_fingerprint():
    from scripts.judge_soak_report import count_paper_setups

    rows = [
        {"setup_key": "EURUSD|BUY|1700000000", "symbol": "EURUSD", "side": "buy", "quantity": 0.01, "sl": 1.0, "tp": 2.0},
        {"setup_key": "EURUSD|BUY|1700000000", "symbol": "EURUSD", "side": "buy", "quantity": 0.01, "sl": 0.9, "tp": 2.1},
    ]
    assert count_paper_setups(rows) == (1, 0)
