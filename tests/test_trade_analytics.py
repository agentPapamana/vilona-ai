import math

import pytest

from core.trade_analytics import closed_trade_metrics


def test_closed_trade_metrics_hand_calculated_and_net_of_fees():
    rows = [
        {"id": 1, "status": "closed", "closed_at": 1, "profit": 100, "commission": -2, "swap": -1},
        {"id": 2, "status": "closed", "closed_at": 2, "profit": -50, "commission": -2, "swap": 0},
        {"id": 3, "status": "closed", "closed_at": 3, "profit": 20, "commission": -1, "swap": 0},
        {"id": 4, "status": "open", "closed_at": 4, "profit": 999},
        {"id": 5, "status": "closed", "closed_at": 5, "profit": None},
    ]
    result = closed_trade_metrics(rows)
    assert result["closed_trade_count"] == 3
    assert result["win_rate"] == pytest.approx(2 / 3)
    assert result["expectancy"] == pytest.approx(64 / 3)
    assert result["profit_factor"] == pytest.approx(116 / 52)
    assert result["gross_profit"] == pytest.approx(116)
    assert result["gross_loss"] == pytest.approx(52)
    assert result["net_profit"] == pytest.approx(64)
    assert result["max_drawdown"] == pytest.approx(52)


def test_empty_closed_trade_set_has_no_claimed_metrics():
    result = closed_trade_metrics([{"status": "open", "profit": 10}])
    assert result["closed_trade_count"] == 0
    for key in ("win_rate", "expectancy", "profit_factor"):
        assert result[key] is None
    assert result["max_drawdown"] == 0


def test_zero_losses_profit_factor_is_infinite_if_profit_exists():
    result = closed_trade_metrics([{"status": "closed", "profit": 10}])
    assert math.isinf(result["profit_factor"])
