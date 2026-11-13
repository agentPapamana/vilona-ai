"""Soak counting must ignore evidence written before the idempotency code.

Legacy `orders` rows carry a NULL setup_key. They cannot demonstrate that the
current code deduplicates setups, so they must not count toward the readiness
gate; the report states how many were dropped.
"""
from core.state_db import StateDB
from scripts.judge_soak_report import count_paper_setups


def _paper_order(symbol: str, setup_key: str | None, created_at: float = 1_700_000_000.0) -> dict:
    return {
        "symbol": symbol,
        "type": "paper",
        "side": "buy",
        "quantity": 0.01,
        "price": 1.0,
        "sl": 0.9,
        "tp": 1.1,
        "status": "filled",
        "created_at": created_at,
        "setup_key": setup_key,
    }


def _db(tmp_path) -> StateDB:
    return StateDB(str(tmp_path / "soak.db"))


def test_null_setup_key_rows_do_not_count_as_distinct_setups(tmp_path):
    db = _db(tmp_path)
    try:
        # Three legacy rows differ only by symbol/price: the old tuple fallback
        # would have counted them as distinct setups.
        db.save_order(_paper_order("EURUSD", None))
        db.save_order(_paper_order("XAUUSD", None))
        db.save_order(_paper_order("GBPUSD", None))
        db.save_order(_paper_order("EURUSD", "EURUSD|BUY|1700000000"))
        rows = [o for o in db.get_orders(limit=100) if o["type"] == "paper"]
    finally:
        db.close()

    distinct, excluded = count_paper_setups(rows)
    assert distinct == 1
    assert excluded == 3


def test_duplicate_setup_keys_still_collapse_for_non_null_keys(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_order(_paper_order("EURUSD", "EURUSD|BUY|1700000000"))
        db.save_order(_paper_order("EURUSD", "XAUUSD|BUY|1700000000"))
        db.save_order(_paper_order("EURUSD", "GBPUSD|BUY|1700000000"))
        # Raw business identity is identical to the first row; only the durable
        # key separates them, which is exactly what the report must count.
        db.save_order(_paper_order("EURUSD", "EURUSD|BUY|1700000100"))
        db.save_order(_paper_order("EURUSD", None))
        rows = [o for o in db.get_orders(limit=100) if o["type"] == "paper"]
    finally:
        db.close()

    distinct, excluded = count_paper_setups(rows)
    assert distinct == 4
    assert excluded == 1


def test_all_null_setup_keys_yields_zero_distinct_setups():
    rows = [_paper_order("EURUSD", None), _paper_order("XAUUSD", None)]
    assert count_paper_setups(rows) == (0, 2)
    assert count_paper_setups([]) == (0, 0)
