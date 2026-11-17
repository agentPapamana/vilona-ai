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
- **Standalone AI Micro-Scalper**: High-frequency 5m strategy with automated multi-timeframe backtest, dynamic `scalp.yaml`, thin TP (1-3 pts), and micro-breakeven.
- **Multi-Strategy Support**: `scalp_5m_agresif`, `pullback`, `breakout`, and `scalp_5m`.
- **SystemOne integration**: Connect to JEV ecosystem (optional).

## 🚀 Quick Start

### Telegram Commands

No commands needed! Use the persistent button menu:

| Button | Description |
|--------|-------------|
| 📊 Status | View open positions, daily P/L, win rate |
| 💰 Balance | Account equity for CCXT or MT5 |
| ⚙️ Control | Start/Stop/Restart scanner |
| 📜 Logs | Recent trading activity |

## 📈 Trading Strategies & Configuration

Vilona AI supports 4 trading strategies selected via the `KINGAI_STRATEGY` environment variable in `.env`.

### 1. `scalp_5m_agresif` (Standalone AI Micro-Scalper) — *Recommended for Gold / Cent*
* **Target Markets:** High-volatility instruments like XAUUSD (Gold), Forex micro pairs on Cent (`XAUUSDc`, `EURUSDc`) and Standard accounts.
* **Core Philosophy:** Ultra-fast 5-minute execution with automated multi-timeframe backtest calibration upon boot and zero runtime AI latency.
* **How It Works:**
  1. **Startup Auto-Backtest:** Upon server boot, automatically pulls 80 candles of 1H, 30m, 15m, and 100 candles of 5m from MT5.
  2. **AI Dynamic Setup:** The LLM Manager ("Scalper") consumes backtest metrics and short-term memory (`memory.json`), dynamically formulating optimal parameters into `scalp.yaml`. If AI is offline, native quantitative calibration automatically applies.
  3. **Thin Take Profit:** Target TP is fixed between **1.0 to 3.0 points** per trade for high win-rate rapid turnover.
  4. **Strict Stop Loss:** Bounded by a strict hard ceiling of **$\le 0.5\%$ account equity**.
  5. **Micro-Breakeven (Micro-BE):** As soon as floating profit reaches **$\ge +1.0$ point**, the stop-loss instantly ratchets to `Entry + 0.1 pt` to lock in capital and eliminate downside risk.
  6. **Volume & Spread Filter:** Automatically vetoes entries if tick volume is dormant ($<40\%$ of 10-bar average) or live spread exceeds `max_spread_points`.

#### How to Configure `scalp_5m_agresif` in `.env`:
```ini
KINGAI_STRATEGY=scalp_5m_agresif
KINGAI_EXCHANGE=mt5
KINGAI_SYMBOLS=XAUUSDc
KINGAI_PAPER_TRADING=true              # Set to false for live demo/real trading
KINGAI_MAX_RISK_PCT=0.5
KINGAI_MAX_CONCURRENT_POSITIONS=1
KINGAI_SCAN_INTERVAL=3                 # 3-second rapid monitoring loop
```
*Dynamic parameters can also be tuned directly in `scalp.yaml`.*

---

### 2. `pullback` (15m Swing Trend Follower) — *Default Core Strategy*
* **Target Markets:** Major Forex pairs (EURUSD, USDJPY) and Crypto (BTC/USDT, ETH/USDT).
* **How It Works:**
  1. **Macro Trend (1H):** Confirmed by the alignment of EMA 21 and EMA 50 on the 1-hour timeframe.
  2. **Entry Trigger (15m):** Enters when price pulls back toward the 15-minute EMA 21 within a $1.2 \times ATR_{15m}$ band.
  3. **Take Profit & Stop Loss:** Wide swing targets ($TP = 3.0 \times ATR_{15m}$, $SL = 1.5 \times ATR_{15m}$) aiming for high Risk-to-Reward ($\ge 1:2$).
  4. **LLM Manager Review:** Each trade candidate can be gated or adjusted by an LLM Judge before execution.

#### How to Configure `pullback` in `.env`:
```ini
KINGAI_STRATEGY=pullback
KINGAI_SYMBOLS=EURUSD,USDJPY,BTC/USDT:USDT
KINGAI_SL_ATR_MULT=1.5
KINGAI_TP_ATR_MULT=3.0
KINGAI_SCAN_INTERVAL=30                # 30-second scan interval
KINGAI_LLM_ENABLED=true                # Optional per-trade LLM approval
```

---

### 3. `breakout` (15m Range Breakout)
* **Target Markets:** Trending pairs undergoing consolidation or session opens.
* **How It Works:**
  1. **Donchian Channel Range:** Identifies the highest high and lowest low of the trailing 20 candles on the 15m timeframe (`KINGAI_BREAKOUT_LOOKBACK=20`).
  2. **Trend Alignment:** BUY triggers only if price breaks above the 20-bar high while 1H EMA 21 > EMA 50; SELL triggers when breaking below 20-bar low while 1H EMA 21 < EMA 50.
  3. **Stops:** Trailing stops and ATR-based profit targets.

#### How to Configure `breakout` in `.env`:
```ini
KINGAI_STRATEGY=breakout
KINGAI_BREAKOUT_LOOKBACK=20
KINGAI_SL_ATR_MULT=1.5
KINGAI_TP_ATR_MULT=3.0
KINGAI_SCAN_INTERVAL=30
```

---

### 4. `scalp_5m` (Legacy 5M Pullback)
* **Target Markets:** Intraday M5 scalping with 15m trend filter.
* **How It Works:**
  1. Uses the 15m EMA 21 / EMA 50 pair for trend direction.
  2. Triggers on a 5-minute pullback within `KINGAI_SCALP_PULLBACK_BAND` $\times ATR_{5m}$ of the 5m EMA 21.
  3. Stops are calculated from 5-minute ATR with per-trade synchronous LLM approval.

#### How to Configure `scalp_5m` in `.env`:
```ini
KINGAI_STRATEGY=scalp_5m
KINGAI_SCALP_PULLBACK_BAND=1.5
KINGAI_SCALP_MAX_SPREAD_ATR=0.35
KINGAI_SCAN_INTERVAL=15
```

---

### 📊 Strategy Comparison Matrix

| Strategy | Primary TF | Secondary TF | Target TP | Max SL Risk | Micro-BE | Execution Latency | Config Source |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `scalp_5m_agresif` | **5m** | **15m & 1H** | **1.0 – 3.0 pts** | **$\le 0.5\%$ Equity** | **Yes (+1.0 pt)** | **< 10ms (Native)** | `scalp.yaml` + Auto-Backtest |
| `pullback` | 15m | 1H | $3.0 \times ATR$ | $1.5 \times ATR$ | Standard (+1 ATR) | 3–15s (LLM Gate) | `.env` (`TradingConfig`) |
| `breakout` | 15m | 1H | $3.0 \times ATR$ | $1.5 \times ATR$ | Standard (+1 ATR) | 3–15s (LLM Gate) | `.env` (`TradingConfig`) |
| `scalp_5m` | 5m | 15m | $3.0 \times ATR$ | $1.5 \times ATR$ | Standard (+1 ATR) | 3–15s (LLM Gate) | `.env` (`TradingConfig`) |

For the exhaustive technical specification, see [docs/strategies-guide.md](docs/strategies-guide.md).

---

## Architecture
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
