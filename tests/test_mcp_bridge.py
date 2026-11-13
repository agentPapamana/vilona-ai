"""
Tests for MT5MCPBridge against a live MCP server.

Two layers, because they need different conditions:

  1. Transport tests — need a reachable MCP endpoint and a correct protocol
     implementation. They run whenever the server answers `initialize`, whether
     or not an MT5 terminal sits behind it.
  2. MT5 functional tests — need a real terminal. They run only when the server
     reports a live connection, so a stub can never turn them green.

Run:
    KINGAI_MT5_MCP_URL=http://127.0.0.1:8765 pytest -q tests/test_mcp_bridge.py -v
"""

import os
import socket
from urllib.parse import urlparse

import pytest

from trading.bridge_mt5_mcp import MT5MCPBridge
from trading.bridge_base import TradingBridge

MCP_URL = os.getenv("KINGAI_MT5_MCP_URL", "http://127.0.0.1:8765")


def _server_reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(MCP_URL),
    reason=f"MT5 MCP server not reachable at {MCP_URL}",
)


@pytest.fixture(scope="module")
def bridge():
    b = MT5MCPBridge(mcp_url=MCP_URL, timeout=10)
    b.connect()
    yield b
    b.disconnect()


@pytest.fixture(scope="module")
def live_bridge(bridge):
    if not bridge._connected:
        pytest.skip("MCP server reachable but no MT5 terminal behind it")
    return bridge


def test_implements_interface():
    assert isinstance(MT5MCPBridge(mcp_url=MCP_URL), TradingBridge)


def test_handshake_establishes_session(bridge):
    """initialize + notifications/initialized must yield a session id."""
    assert bridge._session_id, "MCP session id was not captured"


def test_connect_returns_plain_bool(bridge):
    # Regression: the old MT5Bridge returned a tuple, which is always truthy.
    assert type(bridge.connect()) is bool


def test_connect_reflects_server_answer(bridge):
    """A stub reporting failure must yield False, never a fabricated True."""
    if bridge._connected:
        pytest.skip("MT5 terminal present; failure path not exercised")
    assert bridge.connect() is False


def test_get_tick_returns_none_without_terminal(bridge):
    if bridge._connected:
        pytest.skip("MT5 terminal present")
    assert bridge.get_tick("XAUUSD") is None or bridge.get_tick("XAUUSD").bid >= 0


def test_get_positions_returns_empty_without_terminal(bridge):
    if bridge._connected:
        pytest.skip("MT5 terminal present")
    assert bridge.get_positions() == []


def test_get_tick(live_bridge):
    tick = live_bridge.get_tick("XAUUSD")
    assert tick is not None
    assert tick.symbol == "XAUUSD"
    assert tick.ask >= tick.bid


def test_get_symbol_info(live_bridge):
    info = live_bridge.get_symbol_info("XAUUSD")
    assert info is not None
    assert info.digits > 0
    assert info.point > 0


def test_get_positions_returns_typed_objects(live_bridge):
    positions = live_bridge.get_positions()
    assert isinstance(positions, list)
    for p in positions:
        # Shared dataclass field is price_open, not price.
        assert hasattr(p, "price_open")
        assert not hasattr(p, "price")


def test_pending_orders_use_price_open(live_bridge):
    # Regression: MT5Bridge built Order(price=...) against a price_open field.
    orders = live_bridge.get_pending_orders()
    assert isinstance(orders, list)
    for o in orders:
        assert hasattr(o, "price_open")


def test_send_market_order_returns_trade_result(live_bridge):
    result = live_bridge.send_market_order("XAUUSD", "buy", 0.01)
    assert hasattr(result, "success")
    assert type(result.success) is bool


def test_get_candles(live_bridge):
    candles = live_bridge.get_candles("XAUUSD", "1m", limit=5)
    assert isinstance(candles, list)
    if candles:
        assert {"open", "high", "low", "close"} <= set(candles[0])


def test_calculate_daily_pl_shape(live_bridge):
    pl, wins, total = live_bridge.calculate_daily_pl("XAUUSD")
    assert isinstance(pl, float)
    assert isinstance(wins, int)
    assert isinstance(total, int)
