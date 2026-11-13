"""Route symbols to independent MT5 and CCXT trading bridges."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class MultiMarketBridge:
    """Route each symbol to its venue.

    An explicit entry in ``routes`` always wins. Unlisted symbols fall back to
    symbol shape: a CCXT pair contains ``/`` or ``:``, a forex symbol does not.
    """

    def __init__(self, routes: Dict[str, Any], default_routes: Optional[Dict[str, Any]] = None):
        self.routes = dict(routes)
        self.default_routes = dict(default_routes or {})
        self._connected = False

    def _bridge_for(self, symbol: str):
        if symbol in self.routes:
            return self.routes[symbol]
        if ":" in symbol or "/" in symbol:
            return self.default_routes.get("ccxt")
        return self.default_routes.get("mt5")

    def bridge_for(self, symbol: str):
        bridge = self._bridge_for(symbol)
        if bridge is None:
            raise KeyError(f"No market bridge configured for {symbol}")
        return bridge

    def _bridges(self) -> List[Any]:
        seen, ordered = set(), []
        for bridge in list(self.routes.values()) + list(self.default_routes.values()):
            if id(bridge) not in seen:
                seen.add(id(bridge))
                ordered.append(bridge)
        return ordered

    def connect(self) -> bool:
        """Connect every routed venue; False if any one refuses the connection.

        Partial connectivity would make the scanner believe a symbol is
        tradable while its venue is unreachable, so it fails closed.
        """
        results = []
        for bridge in self._bridges():
            result = bridge.connect()
            results.append(bool(result[0] if isinstance(result, tuple) else result))
        self._connected = bool(results) and all(results)
        return self._connected

    def disconnect(self) -> None:
        for bridge in self._bridges():
            if hasattr(bridge, "disconnect"):
                bridge.disconnect()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def resolve_symbol(self, symbol: str) -> str:
        bridge = self._bridge_for(symbol)
        resolver = getattr(bridge, "resolve_symbol", None)
        return resolver(symbol) if resolver else symbol

    def get_tick(self, symbol: str):
        return self.bridge_for(symbol).get_tick(symbol)

    def get_symbol_info(self, symbol: str):
        return self.bridge_for(symbol).get_symbol_info(symbol)

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100):
        bridge = self.bridge_for(symbol)
        try:
            return bridge.get_candles(symbol, timeframe, limit=limit)
        except TypeError:
            return bridge.get_candles(symbol, timeframe, count=limit)

    def get_positions(self, symbol: Optional[str] = None) -> List[Any]:
        if symbol is not None:
            return self.bridge_for(symbol).get_positions(symbol) or []
        positions: List[Any] = []
        for bridge in self._bridges():
            positions.extend(bridge.get_positions() or [])
        return positions

    def get_account_info(self):
        # Scanner account risk is evaluated per symbol route. Callers that need
        # an aggregate must ask each venue, not take the first account found.
        for bridge in self._bridges():
            return bridge.get_account_info() if hasattr(bridge, "get_account_info") else None
        return None

    def send_market_order(self, symbol: str, *args, **kwargs):
        return self.bridge_for(symbol).send_market_order(symbol, *args, **kwargs)

    def modify_position_sltp(self, ticket: int, symbol: str, *args, **kwargs):
        return self.bridge_for(symbol).modify_position_sltp(ticket, symbol, *args, **kwargs)

    def close_position(self, ticket: int, symbol: str, *args, **kwargs):
        return self.bridge_for(symbol).close_position(ticket, symbol, *args, **kwargs)

    def get_history_deals(self, since_epoch: float, symbol: Optional[str] = None):
        if symbol is not None:
            bridge = self.bridge_for(symbol)
            getter = getattr(bridge, "get_history_deals", None)
            if not callable(getter):
                return None
            try:
                return getter(since_epoch, symbol=symbol)
            except TypeError:
                return getter(since_epoch)
        deals = []
        for bridge in self._bridges():
            if hasattr(bridge, "get_history_deals"):
                current = bridge.get_history_deals(since_epoch)
                if current is None:
                    return None
                deals.extend(current)
        return deals

    def calculate_daily_pl(self, symbol: str):
        return self.bridge_for(symbol).calculate_daily_pl(symbol)

    def get_server_time(self, symbol: str):
        return self.bridge_for(symbol).get_server_time(symbol)

    @property
    def account_currency(self) -> str:
        return "MIXED"
