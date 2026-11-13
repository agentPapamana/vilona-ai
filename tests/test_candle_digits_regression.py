"""Regression guard for the per-symbol price precision fix in the MT5 bridge EA.

Candle feeds must print each requested symbol at its own `SYMBOL_DIGITS`, not the
digits of whatever chart the EA happens to run on. The Python validator already
rejects a quantized feed; these tests pin the two properties that make the
rejection trustworthy: it fires on cross-symbol rounding and stays quiet on a
correctly formatted feed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from candle_quality import assert_usable  # noqa: E402

EA_SOURCE = Path(__file__).resolve().parent.parent / "mql5" / "Experts" / "Vilona" / "MT5BridgeEA.mq5"


def _bars(prices, start=1_700_000_000):
    """Build 5m bars whose OHLC all sit on the given printed prices."""
    return [
        {"time": start + 300 * i, "open": p, "high": p, "low": p, "close": p,
         "tick_volume": 1}
        for i, p in enumerate(prices)
    ]


def test_cross_symbol_rounding_is_rejected():
    # A 5-digit EURUSD series formatted with a 2-digit XAUUSD chart's _Digits.
    quantized = _bars([1.08 + (i % 8) / 100.0 for i in range(400)])
    with pytest.raises(ValueError):
        assert_usable("EURUSD", quantized, 0.00001)


def test_correctly_formatted_feed_passes():
    prices = [1.08500 + (i % 200) * 0.00001 for i in range(400)]
    assert_usable("EURUSD", _bars(prices), 0.00001)


def test_three_digit_symbol_needs_three_printed_decimals():
    prices = [151.234 + (i % 200) * 0.001 for i in range(400)]
    assert_usable("USDJPY", _bars(prices), 0.001)


def test_ea_formats_candles_with_the_requested_symbol_digits():
    source = EA_SOURCE.read_text()
    body = source[source.index("string RatesToJson"):]
    body = body[: body.index("\nstring ")]
    assert "SymbolInfoInteger(symbol, SYMBOL_DIGITS)" in body
    assert "DoubleToString(r[i].open, digits)" in body
    # Chart digits may only survive as the fallback when the symbol lookup fails.
    assert body.count("_Digits") == 1
    assert "if(digits < 0 || digits > 8) digits = _Digits;" in body
