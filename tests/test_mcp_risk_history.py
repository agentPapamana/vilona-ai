import json

import pytest

from trading.bridge_mt5_mcp import MT5MCPBridge


def mcp_result(payload):
    return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}


def bridge_with_response(monkeypatch, response):
    bridge = MT5MCPBridge()
    calls = []

    def call(tool, arguments):
        calls.append((tool, arguments))
        return response

    monkeypatch.setattr(bridge, "_call_tool", call)
    return bridge, calls


def test_server_day_boundary_parses_and_rejects_future_or_error(monkeypatch):
    bridge, calls = bridge_with_response(
        monkeypatch, mcp_result({"success": True, "data": {"server_day_start": 1700000000}})
    )
    assert bridge.get_server_day_start(1700000100) == 1700000000
    assert calls == [("mt5.get_server_day_start", {})]

    bridge, _ = bridge_with_response(
        monkeypatch, mcp_result({"success": True, "data": {"server_day_start": 1700000200}})
    )
    assert bridge.get_server_day_start(1700000100) is None

    bridge, _ = bridge_with_response(
        monkeypatch, mcp_result({"success": False, "error": "history unavailable"})
    )
    assert bridge.get_server_day_start(1700000100) is None


@pytest.mark.parametrize("deals", [
    [{"ticket": 1, "symbol": "EURUSD", "time": 1700000001, "entry": "out", "profit": 8.0, "commission": -1.0, "swap": -0.5, "fee": -0.25}],
    [],
])
def test_history_deals_parses_complete_records_and_since_argument(monkeypatch, deals):
    bridge, calls = bridge_with_response(monkeypatch, mcp_result({"success": True, "data": {"deals": deals}}))
    assert bridge.get_history_deals(1700000000.9) == deals
    assert calls == [("mt5.get_history_deals", {"since": 1700000000})]

def test_history_deals_fails_closed_on_partial_invalid_or_legacy_payload(monkeypatch):
    valid = {"ticket": 1, "symbol": "EURUSD", "time": 1700000001, "entry": "out", "profit": 4, "commission": 0, "swap": 0, "fee": 0}
    invalid_deals = [
        {"profit": 4, "commission": 0, "swap": 0},
        {**valid, "profit": float("nan")},
        {**valid, "entry": "in"},
        {**valid, "time": 1699999999},
    ]
    for deals in invalid_deals:
        bridge, _ = bridge_with_response(monkeypatch, mcp_result({"success": True, "data": {"deals": [deals]}}))
        assert bridge.get_history_deals(1700000000) is None
    for payload in (
        {"success": True, "data": {}},
        {"success": False, "error": "history unavailable"},
        {"success": True, "data": {"deals": "not-a-list"}},
    ):
        bridge, _ = bridge_with_response(monkeypatch, mcp_result(payload))
        assert bridge.get_history_deals(1700000000) is None

    bridge, _ = bridge_with_response(monkeypatch, {"result": {"isError": True, "content": []}})
    assert bridge.get_history_deals(1700000000) is None
def test_mcp_closed_deals_feed_fail_closed_daily_loss_guard(monkeypatch):
    from core.risk_lock import entry_lock_from_bridge

    payload = {"success": True, "data": {"deals": [
        {"ticket": 1, "symbol": "EURUSD", "time": 1700000100, "entry": "out",
         "profit": -18.0, "commission": -1.0, "swap": -1.0, "fee": -1.0}
    ]}}
    bridge, _ = bridge_with_response(monkeypatch, mcp_result(payload))
    monkeypatch.setattr(bridge, "get_server_day_start", lambda now: 1700000000)
    decision = entry_lock_from_bridge(
        bridge, kill_switch=False, now_epoch=1700000200,
        equity_usd=1000, max_daily_loss_pct=2,
    )
    assert decision.allowed is False
    assert decision.reason == "daily loss threshold reached"



def test_invalid_since_never_calls_server(monkeypatch):
    bridge = MT5MCPBridge()
    monkeypatch.setattr(bridge, "_call_tool", lambda *args: pytest.fail("invalid request reached MCP"))
    assert bridge.get_history_deals(0) is None
    assert bridge.get_history_deals(float("nan")) is None
