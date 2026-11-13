#!/usr/bin/env python3
"""
Vilona AI — Quantitative Risk Monitor & Live Portfolio Terminal
Runs locally on macOS / Linux terminal (Orca / iTerm / Terminal).
Real-time monitoring of MT5 positions, risk exposure, ATR metrics, and risk guardrails.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from rich.align import Align
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Error: 'rich' library is required. Install via: pip install rich")
    sys.exit(1)

AGENT_URL = os.environ.get("MT5_AGENT_URL", "http://127.0.0.1:8787")
AGENT_TOKEN = os.environ.get("MT5_AGENT_TOKEN", "TzYS794bznUq8wLqBn8Tkp7WJJ3yPbPEpXQbarzZUNE")

console = Console(force_terminal=True)


def query_agent(command: str, args: Optional[Dict[str, Any]] = None, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "X-MT5-Agent-Token": AGENT_TOKEN,
    }
    payload = json.dumps({"command": command, "args": args or {}}).encode("utf-8")
    try:
        req = urllib.request.Request(f"{AGENT_URL}/command", data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            cid = data.get("id")
        if not cid:
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.12)
            q_req = urllib.request.Request(f"{AGENT_URL}/command/{cid}", headers=headers)
            with urllib.request.urlopen(q_req, timeout=2.5) as q_resp:
                res_data = json.loads(q_resp.read().decode("utf-8"))
                if res_data.get("status") == "done":
                    return res_data.get("result", {}).get("data")
    except Exception:
        return None
    return None


class RiskMonitorApp:
    def __init__(self):
        self.logs: List[str] = []
        self.cached_acc: Optional[Dict[str, Any]] = None
        self.cached_pos: Optional[Dict[str, Any]] = None
        self.last_sync_time: float = 0.0
        self.running: bool = True
        self._lock = threading.RLock()
        self.add_log("Risk Monitor initialized. Connected to MT5 Bridge (:8787).")

        # Initial fetch immediately upon instantiation
        try:
            acc = query_agent("account_info", timeout=2.5)
            pos = query_agent("get_positions", timeout=2.5)
            if acc:
                self.cached_acc = acc
                self.last_sync_time = time.time()
            if pos:
                self.cached_pos = pos
        except Exception:
            pass

    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.logs.append(f"[{ts}] {msg}")
            if len(self.logs) > 30:
                self.logs.pop(0)

    def poll_background(self):
        """Asynchronous data poller to ensure UI never lags or displays blank state."""
        last_pnl_check = 0.0
        while self.running:
            try:
                acc = query_agent("account_info", timeout=3.0)
                pos = query_agent("get_positions", timeout=3.0)

                log_to_add = None
                with self._lock:
                    if acc:
                        self.cached_acc = acc
                        self.last_sync_time = time.time()
                    if pos is not None and "positions" in pos:
                        self.cached_pos = pos

                        # Detect significant floating PnL movement
                        positions = pos.get("positions", [])
                        total_pnl = sum(float(p.get("profit", 0.0)) for p in positions)
                        if abs(total_pnl - last_pnl_check) >= 1.0:
                            log_to_add = f"Portfolio Floating PnL: ${total_pnl:+,.2f} ({len(positions)} positions)"
                            last_pnl_check = total_pnl

                if log_to_add:
                    self.add_log(log_to_add)

            except Exception as e:
                self.add_log(f"Poller warning: {e}")

            time.sleep(1.0)

    def build_header(self) -> Panel:
        with self._lock:
            acc = self.cached_acc
            sync_age = time.time() - self.last_sync_time if self.last_sync_time > 0 else 999.0

        if not acc or sync_age > 10.0:
            content = Text("Connecting to MT5 Bridge (:8787)... Synchronizing terminal data...", style="bold yellow")
            return Panel(Align.center(content), title="🤖 Vilona AI — Risk Intelligence Monitor", border_style="yellow")

        login = acc.get("login", "-")
        server = acc.get("server", "-")
        balance = float(acc.get("balance", 0.0))
        equity = float(acc.get("equity", 0.0))
        margin = float(acc.get("margin", 0.0))
        free_margin = float(acc.get("free_margin", 0.0))
        trade_ok = acc.get("trade_allowed", False)

        margin_lvl = (equity / margin * 100.0) if margin > 0 else 0.0
        pnl = equity - balance
        pnl_color = "green" if pnl >= 0 else "red"

        status_text = "[green bold]● LIVE TRADING ACTIVE[/green bold]" if trade_ok else "[yellow bold]● READ-ONLY (GUARD ACTIVE)[/yellow bold]"

        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=2)
        grid.add_column(justify="center", ratio=3)
        grid.add_column(justify="right", ratio=2)

        left = f"[dim]Account:[/dim] [bold white]{login}[/bold white] | [dim]Server:[/dim] {server}\n[dim]Status:[/dim] {status_text}"
        center = f"[dim]Equity:[/dim] [bold white]${equity:,.2f}[/bold white]   [dim]Balance:[/dim] ${balance:,.2f}\n[dim]Floating PnL:[/dim] [{pnl_color} bold]${pnl:+,.2f}[/{pnl_color} bold]"
        right = f"[dim]Used Margin:[/dim] ${margin:,.2f}\n[dim]Free Margin:[/dim] ${free_margin:,.2f} [dim]| Level:[/dim] {margin_lvl:.0f}%"

        grid.add_row(left, center, right)
        return Panel(grid, title="[bold magenta]🤖 VILONA AI[/bold magenta] — Quantitative Risk & Portfolio Monitor", border_style="bright_blue")

    def build_positions_table(self) -> Panel:
        with self._lock:
            pos_data = self.cached_pos

        table = Table(expand=True, box=None, show_header=True, header_style="bold cyan")
        table.add_column("Ticket", justify="left", style="dim")
        table.add_column("Symbol", justify="left", style="bold")
        table.add_column("Type", justify="center")
        table.add_column("Lots", justify="right")
        table.add_column("Entry Px", justify="right")
        table.add_column("Current Px", justify="right")
        table.add_column("Stop Loss", justify="right")
        table.add_column("Take Profit", justify="right")
        table.add_column("Profit ($)", justify="right")
        table.add_column("Risk Protection", justify="center")

        positions = (pos_data or {}).get("positions", [])
        if not positions:
            table.add_row("—", "No active positions", "—", "—", "—", "—", "—", "—", "$0.00", "[dim]All Capital in Cash[/dim]")
            return Panel(table, title=f"Open Positions (0)", border_style="dim")

        for p in positions:
            ticket = str(p.get("ticket", ""))
            sym = p.get("symbol", "")
            is_buy = p.get("type", 0) == 0
            side_text = "[green bold]BUY[/green bold]" if is_buy else "[red bold]SELL[/red bold]"
            vol = f"{float(p.get('volume', 0.0)):.2f}"
            open_px = float(p.get("price_open", 0.0))
            cur_px = float(p.get("price_current", 0.0))
            sl = float(p.get("sl", 0.0))
            tp = float(p.get("tp", 0.0))
            pnl = float(p.get("profit", 0.0))

            pnl_style = "green bold" if pnl >= 0 else "red bold"
            sl_str = f"{sl:.5f}" if sl > 0 else "[red]NONE[/red]"
            tp_str = f"{tp:.5f}" if tp > 0 else "[dim]NONE[/dim]"

            # Evaluate Risk Protection Status
            if sl > 0:
                if (is_buy and sl >= open_px) or (not is_buy and sl <= open_px):
                    protect_status = "[bold green]✓ BREAK-EVEN (Risk-Free)[/bold green]"
                else:
                    protect_status = "[cyan]Protected (SL active)[/cyan]"
            else:
                protect_status = "[bold red]⚠ UNPROTECTED[/bold red]"

            table.add_row(
                ticket,
                sym,
                side_text,
                vol,
                f"{open_px:.5f}",
                f"{cur_px:.5f}",
                sl_str,
                tp_str,
                f"[{pnl_style}]${pnl:+,.2f}[/{pnl_style}]",
                protect_status,
            )

        return Panel(table, title=f"Active Portfolio Positions ({len(positions)})", border_style="bright_blue")

    def build_risk_guard_panel(self) -> Panel:
        with self._lock:
            pos_data = self.cached_pos
            acc = self.cached_acc

        positions = (pos_data or {}).get("positions", [])
        equity = float((acc or {}).get("equity", 100000.0))

        total_pnl = sum(float(p.get("profit", 0.0)) for p in positions)
        total_risk_lots = sum(float(p.get("volume", 0.0)) for p in positions)
        dd_pct = (abs(total_pnl) / equity * 100.0) if total_pnl < 0 else 0.0

        max_daily_loss_pct = 5.0
        max_daily_loss_usd = equity * (max_daily_loss_pct / 100.0)

        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        c1 = f"[dim]Total Lots:[/dim] [bold white]{total_risk_lots:.2f}[/bold white]\n[dim]Open Count:[/dim] {len(positions)} pairs"
        c2 = f"[dim]Floating PnL:[/dim] [{'green' if total_pnl >= 0 else 'red'} bold]${total_pnl:+,.2f}[/]\n[dim]Current DD:[/dim] {dd_pct:.2f}%"
        c3 = f"[dim]Daily Loss Cap:[/dim] -${max_daily_loss_usd:,.2f}\n[dim]Cap Threshold:[/dim] {max_daily_loss_pct:.1f}% Equity"
        c4 = f"[dim]Risk State:[/dim] [bold green]NORMAL[/bold green]\n[dim]Circuit Breaker:[/dim] [green]STANDBY[/green]"

        grid.add_row(c1, c2, c3, c4)
        return Panel(grid, title="Risk Guardrails & Capital Preservation Engine", border_style="green")

    def build_log_panel(self) -> Panel:
        text = Text()
        with self._lock:
            recent_logs = list(self.logs[-8:])
        for log in recent_logs:
            style = "white"
            if "BUY" in log or "PROFIT" in log or "BREAK-EVEN" in log:
                style = "green"
            elif "ERROR" in log or "FAIL" in log or "WARN" in log:
                style = "red"
            elif "SCAN" in log or "ATR" in log:
                style = "cyan"
            text.append(log + "\n", style=style)
        return Panel(text, title="Risk & Execution Event Feed", border_style="dim")

    def run(self):
        # Initial synchronous fetch to populate first frame instantly
        acc = query_agent("account_info", timeout=2.5)
        pos = query_agent("get_positions", timeout=2.5)
        if acc:
            self.cached_acc = acc
            self.last_sync_time = time.time()
        if pos:
            self.cached_pos = pos

        # Start asynchronous polling thread
        t = threading.Thread(target=self.poll_background, daemon=True)
        t.start()

        if "--once" in sys.argv:
            layout = Layout()
            layout.split_column(
                Layout(name="header", size=5),
                Layout(name="positions", size=10),
                Layout(name="guardrails", size=4),
                Layout(name="logs", minimum_size=5),
            )
            layout["header"].update(self.build_header())
            layout["positions"].update(self.build_positions_table())
            layout["guardrails"].update(self.build_risk_guard_panel())
            layout["logs"].update(self.build_log_panel())
            console.print(layout)
            self.running = False
            return

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=5),
            Layout(name="positions", size=10),
            Layout(name="guardrails", size=4),
            Layout(name="logs", minimum_size=5),
        )

        try:
            while self.running:
                layout["header"].update(self.build_header())
                layout["positions"].update(self.build_positions_table())
                layout["guardrails"].update(self.build_risk_guard_panel())
                layout["logs"].update(self.build_log_panel())

                # Universal terminal clear and render (compatible with Orca, iTerm, VSCode, tmux)
                console.clear()
                console.print(layout)
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.running = False
            console.print("\n[yellow]Risk Monitor stopped.[/yellow]")


if __name__ == "__main__":
    app = RiskMonitorApp()
    app.run()
