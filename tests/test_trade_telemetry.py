"""Unit tests for per-trade telemetry and side/session aggregates."""
import tempfile

import pytest

from core.state_db import StateDB
from core.trade_telemetry import (
    SESSION_LONDON,
    SESSION_NEW_YORK,
    SESSION_OFF_HOURS,
    build_trade_aggregates,
    classify_outcome,
    classify_session,
    net_pnl,
    summarize_trades,
)


@pytest.fixture()
def db():
    return StateDB(tempfile.mktemp(suffix=".db"))


def _trade(ticket, side, profit, commission=0.0, swap=0.0, closed_hour=8.0, **extra):
    row = {
        "ticket": ticket,
        "symbol": "XAUUSDc",
        "side": side,
        "quantity": 1.0,
        "entry_price": 100.0,
        "exit_price": 101.0,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "opened_at": 0.0,
        "closed_at": closed_hour * 3600.0,
        "status": "closed",
    }
    row.update(extra)
    return row


# --- session classification -------------------------------------------------

@pytest.mark.parametrize(
    "hour,expected",
    [(0, SESSION_OFF_HOURS), (6, SESSION_OFF_HOURS), (7, SESSION_LONDON), (11, SESSION_LONDON),
     (12, SESSION_NEW_YORK), (20, SESSION_NEW_YORK), (21, SESSION_OFF_HOURS), (23, SESSION_OFF_HOURS)],
)
def test_classify_session_covers_every_utc_hour_without_overlap(hour, expected):
    assert classify_session(hour * 3600.0) == expected


def test_classify_session_handles_missing_timestamp():
    assert classify_session(None) == SESSION_OFF_HOURS
    assert classify_session(0) == SESSION_OFF_HOURS


# --- accounting -------------------------------------------------------------

def test_net_pnl_subtracts_commission_and_adds_swap():
    assert net_pnl({"profit": 10.0, "commission": 2.5, "swap": -0.5}) == pytest.approx(7.0)


def test_net_pnl_treats_missing_fields_as_zero():
    assert net_pnl({"profit": 4.0}) == pytest.approx(4.0)


def test_classify_outcome_prefers_explicit_broker_result():
    assert classify_outcome({"profit": -5.0, "outcome": "tp"}) == "tp"


def test_classify_outcome_falls_back_to_net_pnl_sign():
    assert classify_outcome({"profit": 3.0, "commission": 0.5}) == "win"
    assert classify_outcome({"profit": 3.0, "commission": 5.0}) == "loss"


# --- bucket summaries -------------------------------------------------------

def test_summarize_trades_uses_net_pnl_for_wins_and_expectancy():
    trades = [
        _trade(1, "buy", 10.0, commission=2.0),
        _trade(2, "buy", -6.0, commission=1.0),
    ]

    stats = summarize_trades(trades)

    assert stats["trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == pytest.approx(50.0)
    assert stats["net_profit"] == pytest.approx(1.0)      # 8 - 7
    assert stats["expectancy"] == pytest.approx(0.5)
    assert stats["costs"] == pytest.approx(3.0)
    assert stats["profit_factor"] == pytest.approx(8.0 / 7.0, rel=1e-3)


def test_summarize_trades_is_empty_when_there_are_no_trades():
    stats = summarize_trades([])

    assert stats["trades"] == 0
    assert stats["expectancy"] == 0.0
    assert stats["profit_factor"] == 0.0
    assert stats["win_rate"] == 0.0


def test_profit_factor_is_zero_when_there_are_no_losses():
    stats = summarize_trades([_trade(1, "sell", 5.0)])

    assert stats["profit_factor"] == 0.0
    assert stats["win_rate"] == pytest.approx(100.0)


# --- aggregates -------------------------------------------------------------

def test_build_trade_aggregates_splits_by_side_without_double_counting():
    trades = [
        _trade(1, "buy", 10.0, closed_hour=8),
        _trade(2, "sell", -4.0, closed_hour=8),
        _trade(3, "buy", -6.0, closed_hour=14),
        _trade(4, "sell", 2.0, closed_hour=22),
    ]

    agg = build_trade_aggregates(trades)

    assert agg["overall"]["trades"] == 4
    assert agg["overall"]["net_profit"] == pytest.approx(2.0)
    assert agg["by_side"]["buy"]["trades"] == 2
    assert agg["by_side"]["buy"]["net_profit"] == pytest.approx(4.0)
    assert agg["by_side"]["sell"]["trades"] == 2
    assert agg["by_side"]["sell"]["net_profit"] == pytest.approx(-2.0)


def test_build_trade_aggregates_splits_by_session_and_sums_to_overall():
    trades = [
        _trade(1, "buy", 10.0, closed_hour=8),
        _trade(2, "sell", -4.0, closed_hour=15),
        _trade(3, "buy", -6.0, closed_hour=23),
        _trade(4, "sell", 2.0, closed_hour=3),
    ]

    agg = build_trade_aggregates(trades)

    assert agg["by_session"][SESSION_LONDON]["trades"] == 1
    assert agg["by_session"][SESSION_NEW_YORK]["trades"] == 1
    assert agg["by_session"][SESSION_OFF_HOURS]["trades"] == 2
    total = sum(bucket["net_profit"] for bucket in agg["by_session"].values())
    assert total == pytest.approx(agg["overall"]["net_profit"])


def test_build_trade_aggregates_backfills_missing_session_from_close_time():
    agg = build_trade_aggregates([_trade(1, "buy", 1.0, closed_hour=8)])

    assert agg["by_session"][SESSION_LONDON]["trades"] == 1


# --- persistence ------------------------------------------------------------

def test_save_trade_persists_every_review_field(db):
    assert db.save_trade(_trade(
        7, "buy", 12.0, commission=2.0, swap=-0.5, closed_hour=8,
        spread=0.35, atr=1.2, session=SESSION_LONDON, outcome="tp",
    ))

    row = db.get_trade_telemetry()[0]

    assert row["entry_price"] == pytest.approx(100.0)
    assert row["exit_price"] == pytest.approx(101.0)
    assert row["profit"] == pytest.approx(12.0)
    assert row["commission"] == pytest.approx(2.0)
    assert row["swap"] == pytest.approx(-0.5)
    assert row["spread"] == pytest.approx(0.35)
    assert row["atr"] == pytest.approx(1.2)
    assert row["outcome"] == "tp"
    assert row["session"] == SESSION_LONDON


def test_get_trade_telemetry_excludes_open_trades_and_filters_by_symbol(db):
    db.save_trade(_trade(1, "buy", 5.0))
    db.save_trade(_trade(2, "sell", 1.0, symbol="EURUSDc"))
    db.save_trade({"ticket": 3, "symbol": "XAUUSDc", "side": "buy", "quantity": 1.0,
                   "entry_price": 10.0, "opened_at": 0.0, "status": "open"})

    assert len(db.get_trade_telemetry()) == 2
    assert [r["ticket"] for r in db.get_trade_telemetry(symbol="EURUSDc")] == [2]


def test_trade_aggregates_from_db_match_helper_output(db):
    db.save_trade(_trade(1, "buy", 10.0, closed_hour=8))
    db.save_trade(_trade(2, "sell", -4.0, closed_hour=8))

    agg = db.get_trade_aggregates(symbol="XAUUSDc")

    assert agg["by_side"]["buy"]["net_profit"] == pytest.approx(10.0)
    assert agg["by_side"]["sell"]["net_profit"] == pytest.approx(-4.0)
    assert agg["overall"]["expectancy"] == pytest.approx(3.0)


def test_save_trade_is_idempotent_per_ticket_with_telemetry(db):
    trade = _trade(5, "buy", 3.0, spread=0.2, atr=1.0, session=SESSION_NEW_YORK)

    assert db.save_trade(trade) is True
    assert db.save_trade(trade) is False
    assert len(db.get_trade_telemetry()) == 1
