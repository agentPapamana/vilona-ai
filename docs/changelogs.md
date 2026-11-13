# Changelog

## 2026-11-14 — Multi-market routing (MT5 + CCXT in one process)

Previously one process spoke to exactly one venue: `KINGAI_EXCHANGE=mt5` meant
every symbol went to MT5, and a CCXT venue meant every symbol went to CCXT. A
mixed book such as *XAUUSD, EURUSD, USDJPY, BTC/USDT:USDT* was impossible.

### Added

- `trading/multi_bridge.py` — `MultiMarketBridge`, one bridge object per venue
  behind a single facade.
  - Explicit routes win; unlisted symbols fall back to shape: a CCXT pair
    contains `/` or `:`, a forex/metals symbol does not.
  - `connect()` connects **every** venue and returns `False` if any one fails.
    Fail-closed on purpose: a half-connected book would let the scanner believe
    an unreachable venue is tradable.
  - `bridge_for(symbol)` gives the scanner a per-symbol handle for quotes,
    candles, metadata, orders, and position actions.
  - `get_positions()` / `get_history_deals()` aggregate across venues;
    `get_account_info()` returns the first account only, never a sum (see
    *Known gaps*).
- `app_server.create_trading_bridge()` builds the router automatically when the
  configured symbol list mixes CCXT pairs and bare forex symbols, or when
  `multi_market` is set. Single-venue setups keep the old path unchanged.

### Changed

- `core/quant_scanner.py`
  - Evaluates each symbol through its own venue bridge: tick, 5m/15m/1h candles,
    symbol info, open positions, and the account/daily-P/L guard used for risk.
  - Weekend entry and flatten windows are now decided **per symbol**
    (`_is_forex_symbol`) instead of per venue (`_is_forex_venue`). Crypto keeps
    trading through the weekend while forex/metals still closes.
  - `manage_open_positions()` routes every tick, candle, close and SL/TP
    modification to the bridge that owns the position.
  - Publishes the quote, candles, and broker digits actually consumed to the
    dashboard.
- `ui/terminal_dashboard.py`
  - Market table now shows a `Venue` column, one row per symbol, and prices at
    the broker's real digit precision instead of a fixed 2 decimals.
  - Header lists all configured symbols and shows `MULTI-MARKET` when routing.
- Tests: `330 passed, 3 skipped`.

### Verified against live demo accounts

`connect()` returned `True` for both venues simultaneously:

| Symbol | Route | Quote | 15m/1h candles | Digits |
|---|---|---|---:|---:|
| XAUUSD | MT5 MCP | 3888.61 / 3888.74 | 60 / 60 | 2 |
| EURUSD | MT5 MCP | 1.10535 / 1.10536 | 60 / 60 | 5 |
| USDJPY | MT5 MCP | 159.913 / 159.916 | 60 / 60 | 3 |
| BTC/USDT:USDT | Bybit CCXT | 69186.8 / 69186.9 | 60 / 60 | 8 |

Accounts seen: MT5 `MetaQuotes-Demo` (login 5058637180, USD, equity 99 141.78)
and Bybit sandbox (USDT, equity 52 432.15). No orders were submitted during
verification; only reads were exercised.

### Known gaps — do not treat as live-ready

1. **Account currency differs per venue** (USD on MT5, USDT on Bybit). Equity
   and daily-loss limits are therefore evaluated per venue account, not as one
   combined figure. A combined risk model needs a defined conversion policy.
2. **Aggregate exposure caps are per venue.** The `max_concurrent_positions`
   check counts the book of the symbol's own venue.
3. `.env` still lists `KINGAI_SYMBOLS=BTC/USDT:USDT,EURUSD`. Enabling all four
   symbols requires
   `KINGAI_SYMBOLS=XAUUSD,EURUSD,USDJPY,BTC/USDT:USDT`.
4. The B9 strategy gate still fails (scalp_5m PF 0.576). The LLM position
   manager supervises open positions; it does not make entry signals
   profitable. Raw market/decision data collection is the prerequisite for
   re-evaluating that gate.

## 2026-11-13 — Closed-trade persistence from broker deal history

### Changed

- `core/quant_scanner.py` records a trade as closed only when the broker deal
  history carries the matching closing deal. If the history is unavailable or
  has no matching deal, nothing is written and a warning is emitted instead of
  a fabricated profit row.
- `_market_deals()`, `_open_tickets()`, `_snapshot_open_positions()`, and
  `_matching_close_deal()` split the detection path so each failure mode is
  explicit (unreadable book, missing deal, symbol mismatch).
- Failed SL/TP readback after an order is logged as a position decision rather
  than silently accepted.

## 2026-11-13 — Bybit demo trading and CCXT market type

### Added

- `KINGAI_EXCHANGE_TYPE` (`spot` | `swap` | `future`) selects the CCXT market
  type; invalid values fail at config load instead of at first order.
- `CCXTBridge.resolve_symbol()` maps a configured base symbol to the loaded
  CCXT contract symbol.
- Without API credentials the bridge configures `fetchPositions: 'usePublic'`
  so scans still run on public data instead of erroring every cycle.

### Changed

- Bybit `sandbox=true` now selects **Demo Trading** (`api-demo.bybit.com`) via
  `enable_demo_trading(True)` rather than testnet. Public market data stays on
  mainnet because Bybit demo has no separate public feed.
- `CCXTBridge.get_account_info()` returns `AccountInfo`, matching the MT5
  bridge contract.

## 2026-11-13 — Fail-closed order protection

### Changed

- `core/order_safety.py` accepts either `(ok, message)` tuples or objects with
  `.success` / `.message`, so MT5 MCP and CCXT results are judged identically.
- When SL cannot be verified the message names why, and states explicitly when
  the position may still be open instead of reporting a bare failure.

## 2026-11-13 — Launcher and dashboard fixes

### Changed

- `scripts/run_app_server.sh` honours an existing `KINGAI_EXCHANGE` instead of
  forcing `mt5`, and warns when stdout is not a TTY (the alternate-screen
  dashboard draws nothing through a pipe, which looked like a hang).
- `TerminalDashboard` resolves daily P/L through `calculate_daily_pl` and shows
  the account currency instead of a hardcoded USDT.

## 2026-11-13 — MT5 MCP bridge integration

### Added

- MT5 over MCP (`KINGAI_MT5_TRANSPORT=mcp`) works on any OS — MCP server →
  agent → MQL5 EA → MT5 terminal.
- Hybrid take-profit ladder and profit-lock logic in the position manager.
