import pytest

from scripts.candle_quality import assert_usable, precision_report


def _bars(prices):
    return [
        {"open": p, "high": p + 0.00002, "low": p - 0.00002, "close": p}
        for p in prices
    ]


def test_accepts_prices_on_advertised_tick_grid():
    bars = _bars([1.10000 + i * 0.00001 for i in range(120)])
    assert_usable("EURUSD", bars, 0.00001)
    assert precision_report(bars, 0.00001)["distinct_close_ticks"] == 120


def _coarse_bars(prices):
    """Two-decimal feed: every price is a whole number of cents, not ticks."""
    return [
        {"open": p, "high": p + 0.01, "low": p - 0.01, "close": p}
        for p in prices
    ]


def test_rejects_quantized_eurusd_prices_against_five_digit_metadata():
    bars = _coarse_bars([1.10 + i * 0.01 for i in range(120)])
    with pytest.raises(ValueError, match="off the tick grid|quantized|coarser"):
        assert_usable("EURUSD", bars, 0.00001)


def test_rejects_low_price_diversity_even_when_each_bar_is_tick_aligned():
    bars = _bars([1.10000 + (i % 4) * 0.00001 for i in range(200)])
    with pytest.raises(ValueError, match="distinct close ticks"):
        assert_usable("EURUSD", bars, 0.00001)


def test_rejects_three_digit_symbol_printed_at_two_decimals():
    bars = _coarse_bars([155.90 + i * 0.01 for i in range(120)])
    with pytest.raises(ValueError, match="coarser than tick_size"):
        assert_usable("USDJPY", bars, 0.001)


def test_rejects_malformed_ohlc_ordering():
    bars = _bars([1.10000 + i * 0.00001 for i in range(120)])
    bars[0]["high"] = bars[0]["close"] - 0.00001
    with pytest.raises(ValueError, match="ordered OHLC"):
        assert_usable("EURUSD", bars, 0.00001)
