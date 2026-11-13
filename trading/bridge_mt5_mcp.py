"""
MT5 MCP Bridge — communicates with an external MT5 MCP server.
Implements TradingBridge interface.

MCP communication uses JSON-RPC 2.0 over HTTP (or stdio).
The MCP server must expose the following tools:
  - mt5.connect
  - mt5.disconnect
  - mt5.get_tick
  - mt5.get_symbol_info
  - mt5.get_positions
  - mt5.get_pending_orders
  - mt5.send_market_order
  - mt5.modify_position_sltp
  - mt5.close_position
  - mt5.get_server_day_start
  - mt5.get_history_deals (closed deals include profit, commission, swap and fee)
  - mt5.calculate_daily_pl
"""

from __future__ import annotations
from typing import List, Optional, Tuple, Dict, Any
import json
import logging
import math
import os
import requests

logger = logging.getLogger(__name__)

from trading.bridge_base import (
    TradingBridge,
    Tick,
    SymbolInfo,
    AccountInfo,
    Position,
    Order,
    TradeResult,
)


_TIMEFRAME_ALIASES = {
    "1": "M1", "1M": "M1", "1MIN": "M1", "1MINUTE": "M1", "1MINUTES": "M1",
    "5": "M5", "5M": "M5", "5MIN": "M5", "5MINUTE": "M5", "5MINUTES": "M5",
    "15": "M15", "15M": "M15", "15MIN": "M15", "15MINUTE": "M15", "15MINUTES": "M15",
    "30": "M30", "30M": "M30", "30MIN": "M30", "30MINUTE": "M30", "30MINUTES": "M30",
    "60": "H1", "60M": "H1", "1H": "H1", "1HR": "H1", "1HOUR": "H1", "1HOURS": "H1",
    "240": "H4", "240M": "H4", "4H": "H4", "4HR": "H4", "4HOUR": "H4", "4HOURS": "H4",
    "1440": "D1", "1440M": "D1", "1D": "D1", "1DAY": "D1", "1DAYS": "D1",
    "10080": "W1", "1W": "W1", "1WEEK": "W1", "1WEEKS": "W1",
    "43200": "MN1", "1MO": "MN1", "1MONTH": "MN1", "1MONTHS": "MN1",
}


def normalize_timeframe(timeframe: str) -> str:
    """Convert scanner/Binance labels to the MQL5 wire format."""
    value = str(timeframe).strip()
    # `1m` means one minute while `1M` means one month in Binance notation.
    if value == "1M":
        return "MN1"
    upper = value.upper()
    if upper in {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"}:
        return upper
    try:
        return _TIMEFRAME_ALIASES[upper]
    except KeyError as exc:
        raise ValueError(f"Unsupported MT5 timeframe: {timeframe!r}") from exc


class MT5MCPBridge(TradingBridge):
    def __init__(
        self,
        mcp_url: str = "http://localhost:8000",
        timeout: int = 30,
        client_token: Optional[str] = None,
    ):
        self.mcp_url = mcp_url.rstrip("/")
        self.timeout = timeout
        # Bearer token presented to the MCP server on order-mutating calls. The
        # bridge only carries it; the server decides whether it is valid.
        self.client_token = client_token if client_token is not None else os.environ.get("MT5_CLIENT_TOKEN", "")
        self._connected = False
        self._session = requests.Session()
        self._session_id: Optional[str] = None
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @staticmethod
    def _decode_response(response: requests.Response) -> Dict[str, Any]:
        """Decode JSON or the single JSON-RPC message in an SSE response."""
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return response.json()
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise ValueError("MCP response contained no data event")

    def _post_rpc(self, method: str, params: Dict[str, Any], *, tool_name: Optional[str] = None) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Method": method,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if tool_name:
            headers["Mcp-Name"] = tool_name
        # Present the client auth token on all requests. The server will refuse
        # order-mutating calls that lack it or carry a bad one.
        if self.client_token:
            headers["Authorization"] = f"Bearer {self.client_token}"
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        response = self._session.post(f"{self.mcp_url}/mcp", json=payload, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        return self._decode_response(response)

    def _ensure_initialized(self) -> None:
        if self._session_id:
            return
        result = self._post_rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "vilona-ai", "version": "0.1.0"},
            },
        )
        if "error" in result or not self._session_id:
            raise ConnectionError(f"MCP initialize failed: {result}")
        # MCP initialization completion is a notification; a 202 response is expected.
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Method": "notifications/initialized",
            "Mcp-Session-Id": self._session_id,
        }
        response = self._session.post(
            f"{self.mcp_url}/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()

    def _call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a FastMCP tool over Streamable HTTP."""
        self._ensure_initialized()
        wire_name = tool_name.removeprefix("mt5.")
        return self._post_rpc(
            "tools/call",
            {"name": wire_name, "arguments": arguments},
            tool_name=wire_name,
        )

    def connect(self) -> bool:
        try:
            result = self._call_tool("mt5.connect", {})
            content = result.get("result", {}).get("content", [])
            if result.get("result", {}).get("isError", False) or not content:
                return False
            data = json.loads(content[0].get("text", "{}"))
            self._connected = bool(data.get("success", False))
            return self._connected
        except Exception as e:
            print(f"MT5 MCP connect failed: {e}")
            return False

    def disconnect(self) -> None:
        try:
            self._call_tool("mt5.disconnect", {})
        except Exception:
            pass
        self._connected = False

    def get_tick(self, symbol: str) -> Optional[Tick]:
        try:
            result = self._call_tool("mt5.get_tick", {"symbol": symbol})
            content = result.get("result", {}).get("content", [])
            if not content:
                return None
            payload = json.loads(content[0].get("text", "{}"))
            # MCP server wraps results as {"success": bool, "data": {...}}.
            if isinstance(payload, dict) and "data" in payload:
                if not payload.get("success", True):
                    print(f"get_tick failed: {payload.get('error') or payload.get('detail')}")
                    return None
                payload = payload["data"] or {}
            if not isinstance(payload, dict):
                return None
            return Tick(
                symbol=payload.get("symbol", symbol),
                bid=float(payload.get("bid", 0)),
                ask=float(payload.get("ask", 0)),
                timestamp_ms=int(payload.get("timestamp_ms") or payload.get("time_msc") or 0),
                open=float(payload.get("open", 0)),
                high=float(payload.get("high", 0)),
                low=float(payload.get("low", 0)),
                volume=float(payload.get("volume", 0)),
            )
        except Exception as e:
            print(f"get_tick failed: {e}")
            return None

    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        try:
            result = self._call_tool("mt5.get_symbol_info", {"symbol": symbol})
            content = result.get("result", {}).get("content", [])
            if not content:
                return None
            payload = json.loads(content[0].get("text", "{}"))
            # MCP server wraps results as {"success": bool, "data": {...}}.
            if isinstance(payload, dict) and "data" in payload:
                if not payload.get("success", True):
                    print(f"get_symbol_info failed: {payload.get('error') or payload.get('detail')}")
                    return None
                payload = payload["data"] or {}
            if not isinstance(payload, dict):
                return None
            return SymbolInfo(
                symbol=payload.get("symbol", symbol),
                point=float(payload.get("point", 0.0001)),
                digits=int(payload.get("digits", 2)),
                lot_step=float(payload.get("lot_step", 0.01)),
                lot_min=float(payload.get("lot_min", 0.001)),
                lot_max=float(payload.get("lot_max", 100.0)),
                position_type_buy=payload.get("position_type_buy", "buy"),
                position_type_sell=payload.get("position_type_sell", "sell"),
                order_type_buy_stop=payload.get("order_type_buy_stop", "buy_stop"),
                order_type_buy_limit=payload.get("order_type_buy_limit", "buy_limit"),
                order_type_sell_stop=payload.get("order_type_sell_stop", "sell_stop"),
                order_type_sell_limit=payload.get("order_type_sell_limit", "sell_limit"),
                deal_entry_out=payload.get("deal_entry_out", "out"),
                tick_value=float(payload.get("tick_value", 0.0)),
                tick_size=float(payload.get("tick_size", 0.0)),
            )
        except Exception as e:
            print(f"get_symbol_info failed: {e}")
            return None

    def get_account_info(self) -> Optional[AccountInfo]:
        try:
            result = self._call_tool("mt5.get_account_info", {})
            content = result.get("result", {}).get("content", [])
            if not content:
                return None
            payload = json.loads(content[0].get("text", "{}"))
            # MCP server wraps results as {"success": bool, "data": {...}}.
            if isinstance(payload, dict) and "data" in payload:
                if not payload.get("success", True):
                    print(f"get_account_info failed: {payload.get('error') or payload.get('detail')}")
                    return None
                payload = payload["data"] or {}
            if not isinstance(payload, dict):
                return None
            return AccountInfo(
                login=int(payload.get("login", 0)),
                server=str(payload.get("server", "")),
                currency=str(payload.get("currency", "")),
                balance=float(payload.get("balance", 0.0)),
                equity=float(payload.get("equity", 0.0)),
                margin=float(payload.get("margin", 0.0)),
                free_margin=float(payload.get("free_margin", 0.0)),
                trade_allowed=bool(payload.get("trade_allowed", False)),
                connected=bool(payload.get("connected", False)),
                build=int(payload.get("build", 0)),
            )
        except Exception as e:
            print(f"get_account_info failed: {e}")
            return None

    def get_positions(self, symbol: str | None = None) -> List[Position]:
        try:
            result = self._call_tool("mt5.get_positions", {"symbol": symbol})
            content = result.get("result", {}).get("content", [])
            if not content:
                return []
            data = json.loads(content[0].get("text", "[]"))
            if isinstance(data, dict) and "data" in data:
                data = data["data"].get("positions", [])
            elif not isinstance(data, list):
                data = []
            positions = []
            for p in data:
                positions.append(
                    Position(
                        ticket=int(p.get("ticket", 0)),
                        symbol=p.get("symbol", ""),
                        type=p.get("type", "buy"),
                        price_open=float(p.get("price_open", 0)),
                        sl=float(p.get("sl", 0)),
                        tp=float(p.get("tp", 0)),
                        volume=float(p.get("volume", 0)),
                        profit=float(p.get("profit", 0)),
                        price_current=float(p.get("price_current", 0)),
                        swap=float(p.get("swap", 0)),
                        magic=int(p.get("magic", 0)),
                        comment=p.get("comment", ""),
                    )
                )
            return positions
        except Exception as e:
            print(f"get_positions failed: {e}")
            return []

    def get_pending_orders(self, symbol: str | None = None, exclude_tickets: Optional[List[int]] = None) -> List[Order]:
        try:
            result = self._call_tool(
                "mt5.get_pending_orders",
                {"symbol": symbol, "exclude_tickets": exclude_tickets or []},
            )
            content = result.get("result", {}).get("content", [])
            if not content:
                return []
            data = json.loads(content[0].get("text", "[]"))
            if isinstance(data, dict) and "data" in data:
                if not data.get("success", True):
                    print(f"get_pending_orders failed: {data.get('error')}")
                    return []
                data = (data.get("data") or {}).get("orders", [])
            if not isinstance(data, list):
                return []
            orders = []
            for o in data:
                orders.append(
                    Order(
                        ticket=int(o.get("ticket", 0)),
                        symbol=o.get("symbol", ""),
                        type=o.get("type", "buy"),
                        price_open=float(o.get("price_open", 0)),
                        volume=float(o.get("volume", 0)),
                        sl=float(o.get("sl", 0)),
                        tp=float(o.get("tp", 0)),
                        time_setup=int(o.get("time_setup", 0)),
                    )
                )
            return orders
        except Exception as e:
            print(f"get_pending_orders failed: {e}")
            return []

    def send_market_order(
        self,
        symbol: str,
        action: str,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "",
        magic: int = 0,
    ) -> TradeResult:
        if sl is None or float(sl) <= 0:
            return TradeResult(
                success=False,
                message="refused: order has no stop-loss",
            )
        try:
            result = self._call_tool(
                "mt5.send_market_order",
                {
                    "symbol": symbol,
                    "action": action,
                    "volume": volume,
                    "sl": sl,
                    "tp": tp,
                    "comment": comment,
                    "magic": magic,
                },
            )
            content = result.get("result", {}).get("content", [])
            if not content:
                return TradeResult(success=False, message="No response from MCP")
            data = json.loads(content[0].get("text", "{}"))
            if not data.get("success"):
                return TradeResult(success=False, message=data.get("error", "Unknown error"))
            payload = data.get("data", data)
            return TradeResult(
                success=True,
                message="Order placed",
                ticket=int(payload.get("ticket", 0)),
            )
        except Exception as e:
            return TradeResult(success=False, message=f"send_market_order exception: {e}")

    def modify_position_sltp(
        self,
        ticket: int,
        symbol: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> TradeResult:
        try:
            result = self._call_tool(
                "mt5.modify_position_sltp",
                {"ticket": ticket, "symbol": symbol, "sl": sl, "tp": tp},
            )
            content = result.get("result", {}).get("content", [])
            if not content:
                return TradeResult(success=False, message="No response from MCP")
            data = json.loads(content[0].get("text", "{}"))
            if not data.get("success"):
                return TradeResult(success=False, message=data.get("error", "Unknown error"))
            return TradeResult(
                success=True,
                message="SL/TP modified",
                new_sl=sl,
                new_tp=tp,
            )
        except Exception as e:
            return TradeResult(success=False, message=f"modify_position_sltp exception: {e}")

    def close_position(
        self,
        ticket: int,
        symbol: str,
        volume: Optional[float] = None,
        comment: str = "",
        magic: int = 0,
    ) -> TradeResult:
        try:
            result = self._call_tool(
                "mt5.close_position",
                {
                    "ticket": ticket,
                    "symbol": symbol,
                    "volume": volume,
                    "comment": comment,
                    "magic": magic,
                },
            )
            content = result.get("result", {}).get("content", [])
            if not content:
                return TradeResult(success=False, message="No response from MCP")
            data = json.loads(content[0].get("text", "{}"))
            if not data.get("success"):
                return TradeResult(success=False, message=data.get("error", "Unknown error"))
            return TradeResult(success=True, message="Position closed")
        except Exception as e:
            return TradeResult(success=False, message=f"close_position exception: {e}")

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 100,
        since: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        try:
            wire_tf = normalize_timeframe(timeframe)
            result = self._call_tool(
                "mt5.get_candles",
                {"symbol": symbol, "timeframe": wire_tf, "limit": limit, "since": since},
            )
            content = result.get("result", {}).get("content", [])
            if not content:
                return []
            parsed = json.loads(content[0].get("text", "[]"))
            if isinstance(parsed, dict) and "data" in parsed:
                return parsed["data"].get("rates", [])
            if isinstance(parsed, list):
                return parsed
            return []
        except Exception as e:
            print(f"get_candles failed: {e}")
            return []

    def get_candles_range(
        self,
        symbol: str,
        timeframe: str,
        from_epoch: int,
        to_epoch: int,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Read a dated window of candles, oldest first.

        The tail read behind `get_candles` is capped at 500 bars by the EA, which
        is about seven days on M15 — far short of the six-month window the B9
        backtest gate requires. This calls the dated `get_candles_range` command
        instead, which the EA serves from `CopyRates(start, stop)`.

        Returns `[]` on any transport or decode failure so a paginating caller
        stops instead of silently treating an error as an empty history.
        """
        try:
            wire_tf = normalize_timeframe(timeframe)
            result = self._call_tool(
                "mt5.get_candles_range",
                {
                    "symbol": symbol,
                    "timeframe": wire_tf,
                    "from_epoch": int(from_epoch),
                    "to_epoch": int(to_epoch),
                    "limit": int(limit),
                },
            )
            content = result.get("result", {}).get("content", [])
            if not content:
                return []
            parsed = json.loads(content[0].get("text", "[]"))
            if isinstance(parsed, dict):
                if not parsed.get("success"):
                    return []
                return parsed.get("data", {}).get("rates", [])
            if isinstance(parsed, list):
                return parsed
            return []
        except Exception as e:
            print(f"get_candles_range failed: {e}")
            return []

    @staticmethod
    def _risk_tool_data(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Decode a successful MCP risk tool response; malformed/error payloads fail closed."""
        try:
            content = result.get("result", {}).get("content", [])
            if result.get("result", {}).get("isError") or len(content) != 1:
                return None
            payload = json.loads(content[0].get("text", ""))
            if not isinstance(payload, dict) or payload.get("success") is not True:
                return None
            data = payload.get("data")
            return data if isinstance(data, dict) else None
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def get_server_day_start(self, now_epoch: float) -> Optional[float]:
        """Return the broker's current server-day boundary, or None if unavailable."""
        try:
            data = self._risk_tool_data(self._call_tool("mt5.get_server_day_start", {}))
            if data is None:
                return None
            boundary, now = float(data["server_day_start"]), float(now_epoch)
            if not math.isfinite(boundary) or not math.isfinite(now) or boundary <= 0 or boundary > now:
                return None
            return boundary
        except Exception:
            logger.exception("MT5 MCP server-day boundary unavailable")
            return None

    def get_history_deals(self, since_epoch: float) -> Optional[List[Dict[str, Any]]]:
        """Return closed account deals since epoch; missing or malformed data is unavailable."""
        try:
            since = float(since_epoch)
            if not math.isfinite(since) or since <= 0:
                return None
            data = self._risk_tool_data(
                self._call_tool("mt5.get_history_deals", {"since": int(since)})
            )
            if data is None or not isinstance(data.get("deals"), list):
                return None
            deals = data["deals"]
            for deal in deals:
                if not isinstance(deal, dict) or deal.get("profit") is None:
                    return None
                if not all(field in deal for field in ("profit", "commission", "swap", "fee", "time", "symbol", "entry")):
                    return None
                for field in ("profit", "commission", "swap", "fee"):
                    if not isinstance(deal[field], (int, float)) or not math.isfinite(float(deal[field])):
                        return None
                if not isinstance(deal["time"], int) or deal["time"] < int(since):
                    return None
                if deal["entry"] not in ("out", "out_by"):
                    return None
                if not isinstance(deal["symbol"], str) or not deal["symbol"]:
                    return None
            return deals
        except Exception:
            logger.exception("MT5 MCP closed-deal history unavailable")
            return None


    def calculate_daily_pl(
        self,
        symbol: str,
        date: Optional[str] = None,
    ) -> Tuple[float, int, int]:
        try:
            result = self._call_tool("mt5.calculate_daily_pl", {"symbol": symbol, "date": date})
            content = result.get("result", {}).get("content", [])
            if not content:
                return (0.0, 0, 0)
            data = json.loads(content[0].get("text", "{}"))
            if isinstance(data, dict) and "data" in data:
                if not data.get("success", True):
                    print(f"calculate_daily_pl failed: {data.get('error')}")
                    return (0.0, 0, 0)
                data = data.get("data") or {}
            return (
                float(data.get("pl", data.get("realized_pl", 0.0)) or 0.0),
                int(data.get("wins", 0) or 0),
                int(data.get("total_closed", data.get("deals", 0)) or 0),
            )
        except Exception as e:
            print(f"calculate_daily_pl failed: {e}")
            return (0.0, 0, 0)
