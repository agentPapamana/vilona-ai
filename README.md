# Vilona AI v2 - AI-Powered Trading Agent

Upgraded decompiled Vilona AI v1. Supports crypto exchanges via CCXT and MT5,
Telegram monitoring, optional multi-agent personas, headless/VPS deployment.

## 📸 Visuals

### Telegram Command Center
The all-in-one mobile dashboard for your bot.
![Telegram UI](https://placehold.co/300x400/1a1a2e/00d9ff?text=Telegram+UI+Demo)

### Terminal Dashboard
Bloomberg-style CLI for VPS and local server monitoring.
![Terminal Dashboard](https://placehold.co/600x400/1a1a2e/00d9ff?text=Terminal+Dashboard+Demo)

### System Tray (Windows)
Lightweight background presence with native notifications.
![System Tray](https://placehold.co/400x120/1a1a2e/00d9ff?text=System+Tray+Demo)

---

## ✨ Features

- **Rich Telegram UI**: Persistent menu buttons, inline control panels, MarkdownV2 styled cards, AI trade alerts with reasoning.
- **Multi-exchange support**: Binance, Bybit, OKX, Kraken (via CCXT) + **MetaTrader 5**.
- **Telegram Command Center**: No more typing commands. Use sleek inline buttons for Start/Stop, Close All, and refresh views.
- **Terminal Dashboard**: Professional CLI using `rich` library. Fixed headers, live P/L tracking, color-coded logs.
- **System Tray App (Windows)**: Discrete background presence with right-click quick access and native toast notifications.
- **Proactive Intelligence**: Bot automatically pushes AI-driven trade alerts with entry reasoning, SL/TP, and P/L updates.
- **Optional headless mode**: Run on VPS without display.
- **Agent personas**: Optional researcher/trader/risk_manager/judge layers.
- **Preserved Vilona AI logic**: Break-even, trailing stop, drift cancellation.
- **SQLite persistence**: Survives restarts, tracks positions & trades.
- **SystemOne integration**: Connect to JEV ecosystem (optional).
- **Single executable**: Nuitka build for Windows (.exe) and Linux.

## 🚀 Quick Start

### Telegram Commands

No commands needed! Use the persistent button menu:

| Button | Description |
|--------|-------------|
| 📊 Status | View open positions, daily P/L, win rate |
| 💰 Balance | Account equity for CCXT or MT5 |
| ⚙️ Control | Start/Stop/Restart scanner |
| 📜 Logs | Recent trading activity |

## Architecture

```
app_server.py (Bridge initialization)
│
├── Trading (bridge_ccxt.py / bridge_mt5.py)
│   └── trade_executor_v2.py (BE/Trailing/Drift)
├── UI / UX Overhaul
│   ├── telegram_controller.py (Command Center)
│   ├── terminal_dashboard.py (Rich CLI)
│   └── system_tray.py (Desktop Presence)
├── Agents (researcher.py, trader.py, risk_manager.py, judge.py)
├── LLM Integration
└── Persistence (SQLite)
```

## MT5 Mac Bridge Synchronization

The MT5 transport uses three components:

```text
Vilona Linux app → MCP server → Mac mt5_agent.py → MT5BridgeEA.ex5 → MetaTrader 5
```

The source of truth for the Mac bridge and EA is this repository:

```text
bridge/mt5_agent.py
mql5/Experts/Vilona/MT5BridgeEA.mq5
```

Use `scripts/bootstrap-bridge.sh` to synchronize those files to the Mac. The
script supports source deployment, Mac-local bootstrap, checksum verification,
automatic backups, launchd restart, and a bridge health check. It never
compiles or attaches an EA and never starts trading.

### Known Mac paths

The current Mac host is `juandisay@shit-mac`:

```bash
MAC_HOST=juandisay@shit-mac
MAC_REPO=/Users/juandisay/apps/vilona-ai
MT5_DATA_DIR="/Users/juandisay/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5"
```

### Deploy from Linux to Mac

```bash
export MAC_HOST=juandisay@shit-mac
export MAC_REPO=/Users/juandisay/apps/vilona-ai
export MT5_DATA_DIR="/Users/juandisay/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5"

scripts/bootstrap-bridge.sh deploy
```

Before changing files, preview the operation:

```bash
DRY_RUN=true scripts/bootstrap-bridge.sh deploy
```

The deploy operation synchronizes only `bridge/` and `mql5/`, copies the EA
source into `MQL5/Experts/Vilona/`, creates timestamped backups, and attempts
to restart the Mac launchd agent when its plist exists. After deployment,
compile `MT5BridgeEA.mq5` in MetaEditor and re-attach it to the intended chart.
The generated `.ex5` binary is not committed or copied from Linux.

### Verify source, installed EA, and bridge health

`status` reports the local checkout when `MAC_HOST` is unset, and the remote Mac
when it is set. Export the same three variables used for deploy, then run:

```bash
scripts/bootstrap-bridge.sh status
```

The source EA and installed EA must have identical SHA-256 checksums.

The agent health endpoint requires the same secret on Linux, the Mac agent,
and the MQL5 EA. Export it in the calling shell; do not commit it:

```bash
export MT5_AGENT_TOKEN='same-secret-used-by-agent-and-EA'
scripts/bootstrap-bridge.sh status
```

A healthy bridge returns:

```json
{"success":true,"agent":true,"ea":true}
```

`agent: true` only proves the Python relay is reachable. `ea: true` proves the
EA has polled the relay recently. HTTP 401 means the token is missing or does
not match; it is not a source synchronization failure.

### Bootstrap on a new Mac

```bash
export SOURCE_URL=git@github.com:agentPapamana/vilona-ai.git
export SOURCE_BRANCH=main
export MAC_REPO="$HOME/apps/vilona-ai"
export MT5_DATA_DIR="/path/to/MetaTrader 5/MQL5"

scripts/bootstrap-bridge.sh bootstrap
```

`bootstrap` clones or updates the checkout, resets it to the selected remote
branch, copies the EA source, optionally restarts a matching launchd service,
and runs the local health/checksum report. The reset is destructive to
uncommitted changes in the Mac checkout; preserve local work before running it.

### Operational checklist

1. Confirm SSH access: `ssh juandisay@shit-mac`.
2. Run `DRY_RUN=true ... deploy`.
3. Run the real deploy.
4. Compile and attach `MT5BridgeEA.mq5` in MetaEditor.
5. Confirm AutoTrading and the EA inputs, including the agent token.
6. Run `status` with `MT5_AGENT_TOKEN` exported.
7. Require `agent=true` and `ea=true` before diagnosing scanner or order flow.
8. Check `logs/vilona.log` for `Autonomous QuantScanner started` and current
   `SCAN SYMBOL` entries.

Never paste `MT5_AGENT_TOKEN`, broker credentials, LLM keys, or `.env` values
into commits, issues, or chat.

## License

Original Vilona AI logic preserved. New code MIT licensed.
