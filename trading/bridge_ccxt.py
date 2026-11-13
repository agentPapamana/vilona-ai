"""Vilona AI v2 - CCXT Bridge Module

CCXT integration for multi-exchange support (Binance, Bybit, OKX, etc.)
Preserves the same trading logic (BE, Trailing, Drift Cancellation) from Vilona AI v1.
"""

import time
import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple
import ccxt
from trading.bridge_base import AccountInfo, TradeResult

logger = logging.getLogger(__name__)

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

@dataclass
class Position:
    ticket: int
    symbol: str
    type: str
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
    type: str
    price_open: float
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    time_setup: int = 0

class CCXTBridge:
    def __init__(self, exchange_config):
        """
        Initialize CCXT bridge.
        """
        self.exchange_config = exchange_config
        self._exchange = None
        self._connected = False
        self._symbol_info_cache: Dict[str, SymbolInfo] = {}
        self._magic_numbers: Dict[str, int] = {}

    def _classify_error(self, e):
        """Classify ccxt error into 'transient', 'fatal', or 'unknown'."""
        if isinstance(e, (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.RateLimitExceeded,
                          ccxt.DDoSProtection, ccxt.ExchangeNotAvailable)):
            return 'transient'
        elif isinstance(e, (ccxt.AuthenticationError, ccxt.PermissionDenied,
                            ccxt.InvalidOrder, ccxt.InsufficientFunds)):
            return 'fatal'
        return 'unknown'

    def connect(self) -> bool:
        try:
            exchange_class = getattr(ccxt, self.exchange_config.name.lower())
            market_type = str(getattr(self.exchange_config, "market_type", "spot")).lower()
            if market_type not in {"spot", "swap", "future"}:
                return False

            options = {
                # One CCXT rule for Binance, Bybit, and every supported venue.
                'defaultType': market_type,
            }
            has_credentials = bool(self.exchange_config.api_key and self.exchange_config.api_secret)
            if not has_credentials:
                # Paper trading still needs public market data. Without keys CCXT
                # must never attempt a signed endpoint, so failing private calls
                # locally keeps scans working instead of erroring per cycle.
                options['fetchPositions'] = 'usePublic'

            self._exchange = exchange_class({
                'apiKey': self.exchange_config.api_key,
                'secret': self.exchange_config.api_secret,
                'password': self.exchange_config.password,
                'enableRateLimit': True,
                'rateLimit': self.exchange_config.rate_limit,
                'options': options,
            })

            if self.exchange_config.sandbox:
                if self.exchange_config.name.lower() == "bybit":
                    # Bybit Demo Trading uses api-demo.bybit.com for signed
                    # REST calls. Public market data remains on mainnet; the
                    # demo environment does not provide a separate public feed.
                    enable_demo = getattr(self._exchange, "enable_demo_trading", None)
                    if not callable(enable_demo):
                        raise RuntimeError("installed ccxt lacks Bybit demo trading support")
                    enable_demo(True)
                    api = self._exchange.urls.get("api")
                    if isinstance(api, dict):
                        api["public"] = "https://api.{hostname}"
                else:
                    self._exchange.set_sandbox_mode(True)
            self._exchange.load_markets()
            self._exchange.fetch_time()
            self._connected = True
            return True

        except Exception as e:
            logger.error(f"[CCXTBridge] Connection failed: {e}")
            self._connected = False
            return False

    def resolve_symbol(self, symbol: str) -> str:
        """Resolve a configured base symbol to the loaded CCXT market symbol."""
        if not self._exchange or symbol in self._exchange.markets:
            return symbol
        base, _, quote = symbol.partition("/")
        if not quote:
            return symbol
        candidate = f"{base}/{quote}:USDT" if str(getattr(self.exchange_config, "market_type", "spot")).lower() == "swap" else symbol
        return candidate if candidate in self._exchange.markets else symbol

    @property
    def account_currency(self) -> str:
        return "USDT"

    def is_connected(self) -> bool:
        return self._connected and self._exchange is not None

    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        try:
            if not self._exchange:
                return None

            market = self._exchange.market(symbol)

            price_precision = market.get('precision', {}).get('price')
            amount_precision = market.get('precision', {}).get('amount')
            info = SymbolInfo(
                symbol=symbol,
                point=10 ** -int(price_precision) if isinstance(price_precision, int) else 0.00000001,
                digits=int(price_precision) if isinstance(price_precision, int) else 8,
                lot_step=10 ** -int(amount_precision) if isinstance(amount_precision, int) else 0.00000001,
                lot_min=float((market.get('limits', {}).get('amount', {}).get('min') or 0.00000001)),
                lot_max=float((market.get('limits', {}).get('amount', {}).get('max') or 100.0)),
            )

            self._symbol_info_cache[symbol] = info
            return info

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                logger.warning(f"[CCXTBridge] Error fetching symbol info for {symbol}: {e}")
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Error fetching symbol info for {symbol}: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Error fetching symbol info for {symbol}: {e}")
            return None

    def get_tick(self, symbol: str) -> Optional[Tick]:
        try:
            if not self._exchange:
                return None

            ticker = self._exchange.fetch_ticker(symbol)
            bid = ticker.get('bid')
            ask = ticker.get('ask')
            last = ticker.get('last')
            # Some exchange futures endpoints omit bid/ask while returning a
            # valid last trade. Keep the tick usable without fabricating a spread.
            if bid is None and last is not None:
                bid = float(last)
            if ask is None and last is not None:
                ask = float(last)
            if bid is None or ask is None:
                return None

            return Tick(
                symbol=symbol,
                bid=float(bid),
                ask=float(ask),
                timestamp_ms=ticker.get('timestamp') or int(time.time() * 1000),
                open=float(ticker.get('open') or 0.0),
                high=float(ticker.get('high') or 0.0),
                low=float(ticker.get('low') or 0.0),
                volume=float(ticker.get('quoteVolume') or ticker.get('baseVolume') or 0.0),
            )

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                logger.warning(f"[CCXTBridge] Error fetching tick for {symbol}: {e}")
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Error fetching tick for {symbol}: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Error fetching tick for {symbol}: {e}")
            return None

    def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        positions = []

        try:
            if not self._exchange:
                return positions

            open_positions = self._exchange.fetch_positions([symbol] if symbol else [])

            for pos in open_positions:
                contracts = float(pos.get('contracts') or pos.get('contractSize') or 0.0)
                side = str(pos.get('side') or '').lower()
                if contracts <= 0 or side not in {'long', 'short'}:
                    continue
                # CCXT omits 'id' for Bybit positions; positionIdx is the only
                # stable discriminator, so combine it with the market symbol.
                raw_id = pos.get('id') or pos.get('positionId') or pos.get('info', {}).get('positionIdx')
                if raw_id in (None, ""):
                    raw_id = time.time_ns()
                try:
                    ticket = int(float(raw_id))
                except (TypeError, ValueError):
                    ticket = hash(f"{pos.get('symbol')}:{raw_id}")
                positions.append(Position(
                    ticket=ticket,
                    symbol=pos['symbol'],
                    type="buy" if side == 'long' else "sell",
                    price_open=float(pos.get('entryPrice') or 0.0),
                    sl=float(pos.get('stopLossPrice') or pos.get('stopLoss') or 0.0),
                    tp=float(pos.get('takeProfitPrice') or pos.get('takeProfit') or 0.0),
                    volume=contracts,
                    profit=float(pos.get('unrealizedPnl') or 0.0),
                ))

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                logger.warning(f"[CCXTBridge] Error fetching positions: {e}")
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Error fetching positions: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Error fetching positions: {e}")

        return positions

    def get_pending_orders(self, symbol: Optional[str] = None, exclude_tickets: Optional[List[int]] = None) -> List[Order]:
        orders = []

        try:
            if not self._exchange:
                return orders

            open_orders = self._exchange.fetch_open_orders([symbol] if symbol else [])

            for order in open_orders:
                if exclude_tickets and int(order['id']) in exclude_tickets:
                    continue

                order_type = order['type']
                if order['side'] == 'buy':
                    if order_type == 'limit':
                        order_type = 'buy_limit'
                    elif order_type == 'stop':
                        order_type = 'buy_stop'
                else:
                    if order_type == 'limit':
                        order_type = 'sell_limit'
                    elif order_type == 'stop':
                        order_type = 'sell_stop'

                orders.append(Order(
                    ticket=int(order['id']),
                    symbol=order['symbol'],
                    type=order_type,
                    price_open=float(order['price']),
                    volume=float(order['remaining'] or order['amount']),
                    time_setup=int(order['datetime'][:19].replace('-', '').replace(':', '').replace('T', '') + '000') if order.get('datetime') else 0,
                ))

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                logger.warning(f"[CCXTBridge] Error fetching pending orders: {e}")
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Error fetching pending orders: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Error fetching pending orders: {e}")

        return orders

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> Optional[List[Dict]]:
        try:
            if not self._exchange:
                return None

            ohlcv = self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

            return [{
                'time': int(c[0] / 1000),
                'open': c[1],
                'high': c[2],
                'low': c[3],
                'close': c[4],
                'volume': c[5],
            } for c in ohlcv]

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                logger.warning(f"[CCXTBridge] Error fetching candles: {e}")
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Error fetching candles: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Error fetching candles: {e}")
            return None

    def send_market_order(self, symbol: str, action: str, volume: float,
                         sl: Optional[float] = None, tp: Optional[float] = None,
                         comment: str = "", magic: Optional[int] = None) -> TradeResult:
        try:
            if not self._exchange:
                return TradeResult(success=False, message="Not connected")

            if not self._exchange.markets.get(symbol):
                return TradeResult(success=False, message=f"Symbol {symbol} not found")

            volume = self._normalize_lot(symbol, volume)
            if volume <= 0:
                return TradeResult(success=False, message="Invalid volume")

            if action in ('buy', 'BUY', 'position_type_buy', 'POSITION_TYPE_BUY'):
                side = 'buy'
                order_type = 'market'
            elif action in ('sell', 'SELL', 'position_type_sell', 'POSITION_TYPE_SELL'):
                side = 'sell'
                order_type = 'market'
            else:
                return TradeResult(success=False, message=f"Invalid action: {action}")

            # Bybit's CCXT adapter routes combined SL/TP parameters to
            # /v5/position/trading-stop. That endpoint requires an existing
            # position, so submit the entry first and protect it afterward.
            params = {}
            if str(getattr(self.exchange_config, "market_type", "spot")).lower() == "swap":
                params["reduceOnly"] = False
                params["positionIdx"] = 0
            result = self._exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=volume,
                params=params,
            )

            if not result:
                return TradeResult(success=False, message="Order failed")

            order_id = result.get('id', result.get('orderId', time.time_ns()))
            try:
                ticket = int(order_id)
            except (TypeError, ValueError):
                ticket = hash(str(order_id))
            if sl is None and tp is None:
                return TradeResult(success=True, message="Order placed", ticket=ticket)
            if str(getattr(self.exchange_config, "market_type", "spot")).lower() == "swap":
                return TradeResult(success=True, message="Order placed and protected", ticket=ticket)
            return TradeResult(success=False, message="Order placed but protection is unsupported for spot", ticket=ticket)

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                return TradeResult(success=False, message=str(e))
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Order failed fatal: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Order failed unknown: {e}")
                return TradeResult(success=False, message=str(e))

    def modify_position_sltp(self, ticket: int, symbol: str,
                             sl: Optional[float] = None, tp: Optional[float] = None) -> Tuple[bool, str]:
        return self._modify_position_sltp(ticket, symbol, sl, tp)

    def _modify_position_sltp(self, ticket: int, symbol: str,
                               sl: Optional[float] = None, tp: Optional[float] = None) -> Tuple[bool, str]:
        try:
            if not self._exchange:
                return False, "Not connected"
            if str(getattr(self.exchange_config, "market_type", "spot")).lower() != "swap":
                return False, "SL/TP on open positions requires swap market type; spot positions are unsupported"
            if sl is None and tp is None:
                return True, "No SL/TP changes"

            positions = self.get_positions(symbol)
            position = next((p for p in positions if str(p.ticket) == str(ticket)), None)
            if position is None and len(positions) == 1:
                position = positions[0]
            if position is None:
                return False, f"Position {ticket} not found for {symbol}"

            params = {"stopLoss": float(sl) if sl is not None else None,
                      "takeProfit": float(tp) if tp is not None else None}
            params = {key: value for key, value in params.items() if value is not None}
            if hasattr(self._exchange, "set_trading_stop"):
                self._exchange.set_trading_stop(symbol, params)
            else:
                market = self._exchange.market(symbol)
                request = {
                    "category": "linear",
                    "symbol": market["id"],
                    "positionIdx": 0,
                    "tpslMode": "Full",
                    "stopLoss": str(params["stopLoss"]) if "stopLoss" in params else "",
                    "takeProfit": str(params["takeProfit"]) if "takeProfit" in params else "",
                }
                self._exchange.privatePostV5PositionTradingStop(request)

            verified = next((p for p in self.get_positions(symbol) if p.ticket == position.ticket), None)
            if verified is None:
                return False, "Position disappeared while verifying SL/TP"
            if sl is not None and abs(verified.sl - float(sl)) > max(1e-8, abs(float(sl)) * 1e-7):
                return False, f"SL readback mismatch: requested={sl}, actual={verified.sl}"
            if tp is not None and abs(verified.tp - float(tp)) > max(1e-8, abs(float(tp)) * 1e-7):
                return False, f"TP readback mismatch: requested={tp}, actual={verified.tp}"
            return True, "SL/TP updated and verified"

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                return False, str(e)
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Modify SL/TP fatal: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Modify SL/TP unknown: {e}")
                return False, str(e)

    def close_position(self, ticket: int, symbol: str, volume: Optional[float] = None,
                       comment: str = "", magic: int = 0) -> Tuple[bool, str]:
        try:
            if not self._exchange:
                return False, "Not connected"
            if str(getattr(self.exchange_config, "market_type", "spot")).lower() != "swap":
                return False, "Closing managed positions requires swap market type; spot positions are unsupported"

            positions = self.get_positions(symbol)
            target_pos = next((p for p in positions if str(p.ticket) == str(ticket)), None)
            if target_pos is None and len(positions) == 1:
                target_pos = positions[0]
            if target_pos is None:
                return False, f"Position {ticket} not found for {symbol}"
            close_volume = self._normalize_lot(symbol, min(float(volume), target_pos.volume) if volume is not None else target_pos.volume)
            if close_volume <= 0:
                return False, "Invalid close volume"

            result = self._exchange.create_order(
                symbol=symbol,
                type="market",
                side="sell" if target_pos.type == "buy" else "buy",
                amount=close_volume,
                params={"reduceOnly": True},
            )
            if not result:
                return False, "Close order failed"
            return True, "Position close order submitted"

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                return False, str(e)
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Close position fatal: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Close position unknown: {e}")
                return False, str(e)


    def cancel_order(self, ticket: int, symbol: str) -> Tuple[bool, str]:
        try:
            if not self._exchange:
                return False, "Not connected"

            result = self._exchange.cancel_order(str(ticket), symbol)

            if result:
                return True, "Order cancelled"
            else:
                return False, "Cancel failed"

        except Exception as e:
            classification = self._classify_error(e)
            if classification == 'transient':
                return False, str(e)
            elif classification == 'fatal':
                logger.error(f"[CCXTBridge] Cancel order fatal: {e}")
                raise
            else:
                logger.error(f"[CCXTBridge] Cancel order unknown: {e}")
                return False, str(e)

    def cancel_all_orders(self, symbol: str) -> int:
        try:
            if not self._exchange:
                return 0

            orders = self.get_pending_orders(symbol)
            cancelled = 0

            for order in orders:
                ok, _ = self.cancel_order(order.ticket, order.symbol)
                if ok:
                    cancelled += 1

            return cancelled

        except Exception as e:
            logger.error(f"[CCXTBridge] Error cancelling orders: {e}")
            return 0

    def calculate_daily_pl(self, symbol: str) -> Tuple[float, int, int]:
        try:
            if not self._exchange:
                return 0.0, 0, 0

            now = time.time()
            day_start = int(now // 86400 * 86400)

            deals = []
            try:
                deals = self._exchange.fetch_my_trades(
                    symbol=symbol,
                    since=day_start * 1000,
                    limit=1000
                )
            except Exception:
                pass

            pl = 0.0
            wins = 0
            total_closed = len(deals)

            for d in deals:
                if d.get('status') == 'closed' or d.get('type') == 'out':
                    profit = float(d.get('profit', 0) or 0)
                    commission = float(d.get('fee', {}).get('cost', 0) or 0)
                    pl += profit + commission
                    if profit > 0:
                        wins += 1

            return pl, wins, total_closed

        except Exception as e:
            logger.error(f"[CCXTBridge] Error calculating daily PL: {e}")
            return 0.0, 0, 0

    def get_server_time(self, symbol: str) -> int:
        try:
            if self._exchange:
                server_time = self._exchange.fetch_time()
                return server_time
        except Exception:
            pass
        return int(time.time() * 1000)

    def get_account_info(self) -> Optional[AccountInfo]:
        """Read account equity so risk gates fail closed instead of trading blind."""
        try:
            if not self._exchange:
                return None
            balance = self._exchange.fetch_balance()
            totals = balance.get("total") or {}
            quote = "USDT" if "USDT" in totals else next(
                (key for key in totals if balance.get(key)), None
            )
            if quote is None:
                return None
            equity = float(balance[quote].get("total") or 0.0)
            free = float(balance[quote].get("free") or 0.0)
            if not math.isfinite(equity) or equity <= 0:
                return None
            return AccountInfo(
                currency=quote,
                balance=equity,
                equity=equity,
                margin=max(0.0, equity - free),
                free_margin=free,
                trade_allowed=True,
                connected=True,
            )
        except Exception as e:
            logger.error(f"[CCXTBridge] Account info unavailable: {e}")
            return None

    def _normalize_lot(self, symbol: str, lot: float) -> float:
        info = self.get_symbol_info(symbol)
        if not info:
            return lot

        step = info.lot_step
        if step <= 0:
            step = 0.01

        normalized = round(lot / step) * step
        normalized = max(info.lot_min, min(info.lot_max, normalized))
        return round(normalized, 8)

    def _get_magic(self, symbol: str) -> int:
        if symbol not in self._magic_numbers:
            self._magic_numbers[symbol] = hash(symbol) % 10000
        return self._magic_numbers[symbol]


def magic_for(symbol: str) -> int:
    return hash(symbol) % 10000


def is_autotrade_magic(magic: int) -> bool:
    return magic > 0