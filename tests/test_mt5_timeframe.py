"""MT5 timeframe wire format: the EA only understands M1/M5/M15/M30/H1/H4/D1/W1/MN1.

Vilona's scanner uses Binance-style labels ("15m", "1h"). The EA silently falls
back to M1 for unknown strings, so candles came back at 1-minute spacing for
every timeframe — corrupting EMA/ATR inputs. Normalisation must happen client
side, before the wire call.
"""
from __future__ import annotations

import pytest

from trading.bridge_mt5_mcp import normalize_timeframe


@pytest.mark.parametrize("given,expected", [
    # already wire format
    ("M1", "M1"),
    ("M5", "M5"),
    ("M15", "M15"),
    ("M30", "M30"),
    ("H1", "H1"),
    ("H4", "H4"),
    ("D1", "D1"),
    ("W1", "W1"),
    ("MN1", "MN1"),
    # vilona / binance style
    ("1m", "M1"),
    ("5m", "M5"),
    ("15m", "M15"),
    ("30m", "M30"),
    ("1h", "H1"),
    ("4h", "H4"),
    ("1d", "D1"),
    ("1w", "W1"),
    ("1M", "MN1"),
    # bare minutes
    ("1", "M1"),
    ("5", "M5"),
    ("15", "M15"),
    ("30", "M30"),
    ("60", "H1"),
    ("240", "H4"),
    ("1440", "D1"),
    # case / whitespace tolerance
    ("m15", "M15"),
    ("h1", "H1"),
    (" 15m ", "M15"),
])
def test_normalize_timeframe(given, expected):
    assert normalize_timeframe(given) == expected


def test_unknown_timeframe_raises_instead_of_silent_m1():
    # A silent fallback to M1 is what corrupted every indicator; refuse it.
    with pytest.raises(ValueError):
        normalize_timeframe("2h")


def test_unknown_timeframe_raises_for_garbage():
    with pytest.raises(ValueError):
        normalize_timeframe("banana")
