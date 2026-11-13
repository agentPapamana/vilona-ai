"""
Abstract base interface for trading bridges.
All concrete bridges (CCXT, MT5-MCP, Paper) must implement this.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any


@dataclass
class Tick:
    symbol: str
    bid: float
    ask: float
    timestamp_ms: int
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0


@dataclass
class SymbolInfo:
    symbol: str
    point: float = 0.0001
    digits: int = 2
    lot_step: float = 0.01
    lot_min: float = 0.001
    lot_max: float = 100.0
    position_type_buy: str = "buy"
    position_type_sell: str = "sell"
    order_type_buy_stop: str = "buy_stop"
    order_type_buy_limit: str = "buy_limit"
    order_type_sell_stop: str = "sell_stop"
    order_type_sell_limit: str = "sell_limit"
    deal_entry_out: str = "out"
    # Monetary value of one point move per 1.0 lot, in account currency.
    # When > 0 this is the authoritative source for lot sizing.
    tick_value: float = 0.0
    tick_size: float = 0.0


@dataclass
class AccountInfo:
    login: int = 0
    server: str = ""
    currency: str = ""
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    trade_allowed: bool = False
    connected: bool = False
    build: int = 0


@dataclass
class Position:
    ticket: int
    symbol: str
    type: str  # "buy" | "sell"
    price_open: float
    sl: float
    tp: float
    volume: float
    profit: float = 0.0
    price_current: float = 0.0
    swap: float = 0.0
    magic: int = 0
    comment: str = ""


@dataclass
class Order:
    ticket: int
    symbol: str
    type: str  # "buy", "sell", "buy_stop", "sell_stop", "buy_limit", "sell_limit"
    price_open: float
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    time_setup: int = 0


@dataclass
class TradeResult:
    success: bool
    message: str
    ticket: Optional[int] = None
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None


class TradingBridge(ABC):
    @abstractmethod
    def connect(self) -> bool:
        """Connect to the trading backend. Returns True on success."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the trading backend."""
        ...

    @abstractmethod
    def get_tick(self, symbol: str) -> Optional[Tick]:
        """Get current bid/ask for a symbol."""
        ...

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """Get symbol metadata (point, digits, etc.)."""
        ...

    @abstractmethod
    def get_positions(self, symbol: str | None = None) -> List[Position]:
        """Get open positions. If symbol is None, return all."""
        ...

    @abstractmethod
    def get_pending_orders(self, symbol: str | None = None, exclude_tickets: Optional[List[int]] = None) -> List[Order]:
        """Get pending orders. If symbol is None, return all. Exclude tickets if provided."""
        ...

    @abstractmethod
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
        """Send market order. action: "buy" or "sell"."""
        ...

    @abstractmethod
    def modify_position_sltp(
        self,
        ticket: int,
        symbol: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> TradeResult:
        """Modify SL/TP of an open position."""
        ...

    @abstractmethod
    def close_position(
        self,
        ticket: int,
        symbol: str,
        volume: Optional[float] = None,
        comment: str = "",
        magic: int = 0,
    ) -> TradeResult:
        """Close an open position. If volume is None, close all."""
        ...

    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 100,
        since: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get OHLCV candles. timeframe: "1m", "5m", "1h", etc."""
        ...

    def get_history_deals(self, since_epoch: float):
        """Return authoritative account-currency closed-deal records since epoch, or None if unavailable."""
        return None

    def get_server_day_start(self, now_epoch: float):
        """Return broker server's current-day start epoch, or None if unavailable."""
        return None

    @abstractmethod
    def calculate_daily_pl(
        self,
        symbol: str,
        date: Optional[str] = None,
    ) -> Tuple[float, int, int]:
        """Calculate P/L for today (or given date). Returns (pl, wins, total_closed)."""
        ...
