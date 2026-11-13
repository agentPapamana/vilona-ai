"""Judge audit rows retain the model used for each decision."""
from core.state_db import StateDB


def test_judge_decision_records_model(tmp_path):
    db = StateDB(path=str(tmp_path / "state.db"))
    try:
        db.save_judge_decision({
            "symbol": "XAUUSD",
            "action": "SELL",
            "verdict": "APPROVE",
            "model": "ag/gemini-3.8-flash-medium",
        })
        row = db.get_judge_decisions(limit=1)[0]
        assert row["model"] == "ag/gemini-3.8-flash-medium"
    finally:
        db.close()


def test_judge_stats_can_filter_by_model(tmp_path):
    db = StateDB(path=str(tmp_path / "state.db"))
    try:
        for model, verdict in [
            ("old-model", "ERROR"),
            ("new-model", "APPROVE"),
            ("new-model", "VETO"),
        ]:
            db.save_judge_decision({
                "symbol": "XAUUSD", "action": "SELL",
                "verdict": verdict, "model": model,
            })
        stats = db.get_judge_decision_stats(model="new-model")
        assert stats == {
            "total": 2, "approved": 1, "vetoed": 1,
            "errors": 0, "no_verdict": 0,
        }
    finally:
        db.close()
