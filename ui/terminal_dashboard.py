from __future__ import annotations

import time
import logging
from typing import Any, List, Dict
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich.columns import Columns

logger = logging.getLogger(__name__)
console = Console()

class TerminalDashboard:
    def __init__(self, bridge: Any, config: Any):
        self.bridge = bridge
        self.config = config
        self._logs: List[str] = []
        self._agents_logs: List[str] = []
        self._market: dict[str, dict[str, Any]] = {}

    def update_market(self, symbol: str, tick: Any, candles: dict[str, Any] | None = None,
                      symbol_info: Any = None) -> None:
        """Store the latest quote the scanner consumed, one row per symbol."""
        if tick is None:
            return
        digits = getattr(symbol_info, "digits", None)
        payload = {"bid": getattr(tick, "bid", 0.0), "ask": getattr(tick, "ask", 0.0),
                   "time": getattr(tick, "timestamp_ms", 0), "candles": candles or {},
                   "digits": int(digits) if isinstance(digits, int) and 0 <= digits <= 10 else 2,
                   "venue": self._venue_for(symbol)}
        self._market[symbol] = payload

    def _venue_for(self, symbol: str) -> str:
        bridge_for = getattr(self.bridge, "bridge_for", None)
        if bridge_for is not None:
            try:
                bridge = bridge_for(symbol)
            except (KeyError, TypeError):
                return "—"
        else:
            bridge = self.bridge
        if "mt5" in type(bridge).__name__.lower():
            return "MT5"
        exchange_cfg = getattr(self.config, "exchange", None)
        name = str(getattr(exchange_cfg, "name", "CCXT")).upper()
        market_type = str(getattr(exchange_cfg, "market_type", "")).upper()
        return f"{name}/{market_type}" if market_type else name

    def add_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._logs.append(f"[{timestamp}] {message}")
        if len(self._logs) > 50:
            self._logs.pop(0)

    def add_agent_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._agents_logs.append(f"[{timestamp}] {message}")
        if len(self._agents_logs) > 50:
            self._agents_logs.pop(0)

    def _get_header(self) -> Panel:
        mode = "LIVE" if not getattr(self.config, 'paper_trading', True) else "PAPER"
        exchange_cfg = getattr(self.config, "exchange", None)
        exchange = str(getattr(exchange_cfg, "name", "bridge")).upper()
        market_type = str(getattr(exchange_cfg, "market_type", "")).upper()
        venue = "MULTI-MARKET" if hasattr(self.bridge, "bridge_for") else (f"{exchange}/{market_type}" if exchange != "MT5" and market_type else exchange)
        symbols = getattr(getattr(self.config, "trading", None), "symbols", None) or []
        symbol = ", ".join(symbols) if symbols else "—"

        try:
            pl_method = getattr(self.bridge, "calculate_daily_pl", None) or getattr(self.bridge, "calc_daily_pl", None)
            pl, _, _ = pl_method(symbol) if pl_method else (0.0, 0, 0)
            currency = getattr(self.bridge, "account_currency", None) or "USDT"
            pnl_color = "green" if pl > 0 else "red" if pl < 0 else "white"
            pnl_text = f"[{pnl_color}]P/L: {pl:+.2f} {currency}[/{pnl_color}]"
        except Exception:
            pnl_text = "P/L: ---"

        title = Text("🤖 Vilona AI v2", style="bold magenta")
        content = Text(f" {mode} | {venue} | {symbol} | Status: RUNNING | {pnl_text} ", style="white")
        
        return Panel(
            Align.center(content),
            title=title,
            border_style="bright_blue",
            padding=(0, 1)
        )

    def _get_log_table(self) -> Table:
        table = Table(show_header=False, box=None, expand=True)
        for log in self._logs[-15:]:
            # Simple color coding based on keywords
            style = "white"
            if "BUY" in log.upper(): style = "green"
            elif "SELL" in log.upper(): style = "red"
            elif "CLOSE" in log.upper(): style = "cyan"
            elif "SCAN" in log.upper(): style = "yellow"
            
            table.add_row(Text(log, style=style))
        return table
    def _get_market_table(self) -> Table:
        table = Table(show_header=True, box=None, expand=True)
        for heading in ("Venue", "Symbol", "Bid", "Ask", "Spread", "5m", "15m", "1h"):
            table.add_column(heading, justify="right" if heading not in {"Venue", "Symbol"} else "left")
        for symbol, data in self._market.items():
            bid, ask = float(data.get("bid") or 0), float(data.get("ask") or 0)
            spread = ((ask - bid) / bid * 100) if bid > 0 else 0.0
            closes = data.get("candles", {})
            digits = max(0, min(10, int(data.get("digits", 5))))
            values = [str(data.get("venue", "—")), str(symbol), f"{bid:.{digits}f}", f"{ask:.{digits}f}", f"{spread:.4f}%"]
            values.extend(f"{float((closes.get(tf) or {}).get('close', 0)):.{digits}f}" for tf in ("5m", "15m", "1h"))
            table.add_row(*values)
        return table


    def _get_agents_table(self) -> Table:
        table = Table(show_header=False, box=None, expand=True)
        for log in self._agents_logs[-15:]:
            upper = log.upper()
            style = "white"
            if "TRADER" in upper:
                style = "green"
            elif "RISK_MANAGER" in upper or "RISK" in upper:
                style = "yellow"
            elif "JUDGE" in upper:
                style = "magenta"
            table.add_row(Text(log, style=style))
        return table

    def _get_footer(self) -> Panel:
        uptime = "Calculating..."
        # Simplified uptime
        pos_count = len(self.bridge.get_positions()) if hasattr(self.bridge, 'get_positions') else 0
        
        content = Text(
            f"Uptime: {int(time.time() - self._stats_start)}s | "
            f"Positions: {pos_count} | "
            f"Mode: {'PAPER' if getattr(self.config, 'paper_trading', True) else 'LIVE'}",
            style="dim white"
        )
        return Panel(Align.center(content), border_style="dim")

    def start(self):
        self._stats_start = time.time()
        self.running = True
        
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="market", size=5),
            Layout(name="body", minimum_size=10),
            Layout(name="agents", minimum_size=8),
            Layout(name="footer", size=3),
        )
        with Live(layout, refresh_per_second=1, screen=True) as live:
            while self.running:
                layout["header"].update(self._get_header())
                layout["market"].update(Panel(self._get_market_table(), title="Crypto Market Data (consumed)", border_style="cyan"))
                layout["body"].update(Panel(self._get_log_table(), title="Live Trading Feed", border_style="bright_blue"))
                layout["agents"].update(Panel(self._get_agents_table(), title="Agent Activity (trader / risk_manager / judge)", border_style="magenta"))
                layout["footer"].update(self._get_footer())
                time.sleep(0.5)

    def stop(self):
        self.running = False
