"""Soak readiness gate: decide whether PAPER has produced enough evidence.

Going live on thin evidence is the failure mode this gate exists to prevent. The
rule is deliberately blunt: a minimum number of real judgments AND a low error
rate, or the answer is NOT READY with the specific blocker named.
"""

from __future__ import annotations

from core.soak_readiness import evaluate_soak_readiness


def _stats(total=0, approved=0, vetoed=0, errors=0, no_verdict=0):
    return {
        "total": total, "approved": approved, "vetoed": vetoed,
        "errors": errors, "no_verdict": no_verdict,
    }


def test_not_ready_when_no_judgments_recorded():
    r = evaluate_soak_readiness(_stats(), paper_trades=0)
    assert r["ready"] is False
    assert any("judgment" in b for b in r["blockers"])


def test_not_ready_when_paper_trades_below_minimum():
    r = evaluate_soak_readiness(
        _stats(total=40, approved=10, vetoed=28, errors=2), paper_trades=12,
    )
    assert r["ready"] is False
    assert any("paper trade" in b for b in r["blockers"])


def test_not_ready_when_error_rate_too_high():
    r = evaluate_soak_readiness(
        _stats(total=100, approved=40, vetoed=45, errors=15),
        paper_trades=60, min_judgments=50, min_paper_trades=50, max_error_rate=0.10,
    )
    assert r["ready"] is False
    assert any("error rate" in b.lower() for b in r["blockers"])


def test_ready_when_all_thresholds_met():
    r = evaluate_soak_readiness(
        _stats(total=100, approved=45, vetoed=52, errors=3),
        paper_trades=60, min_judgments=50, min_paper_trades=50, max_error_rate=0.10,
    )
    assert r["ready"] is True
    assert r["blockers"] == []
    assert r["error_rate"] == 0.03


def test_no_verdict_rows_are_reported_as_a_blocker():
    r = evaluate_soak_readiness(
        _stats(total=100, approved=45, vetoed=45, errors=0, no_verdict=10),
        paper_trades=60, min_judgments=50, min_paper_trades=50, max_error_rate=0.10,
    )
    assert r["ready"] is False
    assert any("no verdict" in b.lower() for b in r["blockers"])


def test_duplicate_paper_rows_are_a_blocker_not_evidence():
    """185 rows / 10 distinct setups must not read as 185 tests of the path."""
    r = evaluate_soak_readiness(
        _stats(total=100, approved=45, vetoed=52, errors=3),
        paper_trades=10, paper_rows=185,
        min_judgments=50, min_paper_trades=5, max_error_rate=0.10,
    )
    assert r["ready"] is False
    assert r["duplicate_rows"] == 175
    assert any("duplicate paper evidence" in b for b in r["blockers"])


def test_veto_heavy_sample_is_a_concern_not_a_pass():
    """100 judgments but 99 vetoes means the strategy never trades; not ready."""
    r = evaluate_soak_readiness(
        _stats(total=100, approved=1, vetoed=99, errors=0),
        paper_trades=60, min_judgments=50, min_paper_trades=50, max_error_rate=0.10,
    )
    assert r["ready"] is False
    assert any("approval" in b.lower() or "veto" in b.lower() for b in r["blockers"])
