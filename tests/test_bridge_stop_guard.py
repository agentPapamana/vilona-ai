"""Bridge-level guard: an order must never leave the client without a stop-loss.

The MCP server refuses naked orders at its boundary, which is the last line of
defence. This suite proves the client bridge refuses them too, so a caller that
bypasses ``place_protected_order`` fails locally instead of relying on the
server and burning a round-trip.

No network is used: ``_call_tool`` is replaced with a recorder.
"""

import pytest

from trading.bridge_mt5_mcp import MT5MCPBridge


class RecordingBridge(MT5MCPBridge):
    def __init__(self):
        super().__init__(mcp_url="http://127.0.0.1:9", timeout=1)
        self.calls = []

    def _call_tool(self, tool, arguments):
        self.calls.append((tool, arguments))
        return {
            "result": {
                "content": [
                    {"type": "text", "text": '{"success": true, "data": {"ticket": 4242}}'}
                ]
            }
        }


@pytest.fixture()
def bridge():
    return RecordingBridge()


@pytest.mark.parametrize("sl", [None, 0.0, -1.0])
def test_send_market_order_refuses_missing_stop(bridge, sl):
    result = bridge.send_market_order("XAUUSD", "buy", 0.01, sl=sl)

    assert result.success is False
    assert "stop-loss" in result.message.lower()
    assert bridge.calls == [], "no request may reach the MCP server"


def test_send_market_order_allows_valid_stop(bridge):
    result = bridge.send_market_order("XAUUSD", "buy", 0.01, sl=4000.0)

    assert result.success is True
    assert result.ticket == 4242
    assert len(bridge.calls) == 1
    tool, arguments = bridge.calls[0]
    assert tool == "mt5.send_market_order"
    assert arguments["sl"] == 4000.0
