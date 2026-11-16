# Evidence log — safety decisions and bounded strategy research

Append-only. Each entry names the timestamp, the action, the observed state, and the evidence source. Superseded entries stay; a correction is a new entry.

## 2026-11-13 — PAPER rollback during bounded research

- **Action:** verified that the MetaQuotes-Demo account `5058637180` had balance/equity `99172.72` and no open positions. Armed `/tmp/vilona-STOP_ENTRIES`, sent `SIGTERM` to `app_server.py` PID `384368`, confirmed the process exited, and rechecked bridge positions: `[]`.
- **Action:** set `vilona_PAPER_TRADING=true` in `.env` (backup written beside it as `.env.bak-canary-<timestamp>`), removed the stop file, and restarted through `scripts/run_app_server.sh`.
- **Evidence:** new `app_server.py` process environment reports `vilona_PAPER_TRADING=true`, `vilona_STRATEGY=scalp_5m`, `vilona_SYMBOLS=XAUUSD`; startup log line `SAFETY CONFIG: ... paper_trading=True` at `2026-11-12 23:14:20`; log continues `XAUUSD: no signal`. No broker order is permitted while PAPER is effective.
- **Bridge note:** first heartbeat after restart reported `bridge heartbeat unknown`, then `BRIDGE RECOVERED: tick feed resumed` after the first direct XAUUSD tick (`bid 3855.87`, `ask 3856.00`) was observed. No order was involved.

## 2026-11-13 — first cost-versus-volatility diagnostic (does not pass gate)

`cost_volatility_test.py`, XAUUSD only, 190-day verified M5 snapshot, fixed spread `0.30` and commission `$7/lot`, production `scalp_5m` rule with its confidence filter.

| cost/ATR bucket | trades | expectancy $ | PF | win % | OOS trades | OOS expectancy $ | OOS PF |
|---|---:|---:|---:|---:|---:|---:|---:|
| `[0.10,0.15)` | 224 | -7.61 | 0.484 | 18.8 | 132 | -3.92 | 0.609 |
| `[0.15,0.20)` | 14 | -1.32 | 0.825 | 21.4 | 12 | -1.14 | 0.784 |
| all | 2,214 | -3.38 | 0.736 | 21.3 | 1,116 | -1.79 | 0.789 |

- No bucket shows a positive result; the diagnostic does not support “cost alone explains the loss.”
- USDJPY was not run: its M5 feed has two printed decimals despite metadata claiming three. EURUSD has the same precision failure. The EA digits fix is source-only until compiled and deployed.
- Fixed spread and 190 days fail the pre-registered gate; these figures are diagnostic, not a candidate result.
- **Correction:** the first full-snapshot cost screen used an approximate simulator and did not match `backtest_strategy.py`; those early bucket counts and PFs were invalid. The script now matches `run_symbol_scalp`: a regression test asserts identical closed-trade timestamps and P/L on a fixed 1,500-bar real XAUUSD fixture. The test passes.
- **Corrected full-snapshot rerun:** the same bucket table was reproduced after that parity correction. Independent production command `.venv/bin/python scripts/backtest_strategy.py --months 6 --candles-dir-m5 data/candles_m5_verified --strategy scalp_5m --walk-forward --symbols XAUUSD` reported 2,214 trades, expectancy `-$3.38`, PF `0.736`; OOS 1,108 trades, expectancy `-$4.12`, PF `0.778`; B9 FAIL. The cost screen splits those same production trades. Its OOS count differs because it uses a timestamp midpoint (not the production backtest's equal-count trade split); use the B9 output for the gate. Bucket results remain descriptive only.

### Corrected bucket table (same rerun)

| cost/ATR bucket | trades | expectancy $ | PF | win % | OOS trades | OOS expectancy $ | OOS PF |
|---|---:|---:|---:|---:|---:|---:|---:|
| `[0.10,0.15)` | 224 | -7.61 | 0.484 | 18.8 | 132 | -3.92 | 0.609 |
| `[0.15,0.20)` | 14 | -1.32 | 0.825 | 21.4 | 12 | -1.14 | 0.784 |
| all | 2,214 | -3.38 | 0.736 | 21.3 | 1,116 | -1.79 | 0.789 |


## 2026-11-13 — prerequisites that block a gate pass

1. Compile/redeploy `MT5BridgeEA.mq5` (`RatesToJson` now uses per-symbol `SYMBOL_DIGITS`) and run the EURUSD → USDJPY → XAUUSD symbol-switch regression. No compiler/terminal was reachable from this host at the time of writing.
2. Refetch and validate EURUSD/USDJPY history with `scripts/candle_quality.py`.
3. Obtain three years of verified OHLC, historical per-bar bid/ask spread, and commission/swap data, or record that no-go limitation.
4. Implement and run at least 500 matched null simulations per symbol with recorded seeds. The existing unconditioned fixed-cost harness does not meet this requirement.
5. Freeze the pre-registered gate in version control **before** viewing fresh results.

## 2026-11-13 — small null-run diagnostic; 500-run run not completed

The existing `null_signal_test.py` was invoked with 500 XAUUSD runs, 190-day candles, period 50, fixed seed `20261112`, fixed spread, and default commission. It ran for 30 minutes without returning output and was cancelled. It does not match candidate entry timestamps/trade count, so even a completed result would not satisfy the pre-registered matched-null gate. A five-run diagnostic completed in 7.6 seconds (median PF `0.689`, p05 `0.632`, p95 `0.739`; median 715 trades/run; median expectancy `-$6.28`), but is far too small for inference and is recorded only as a runtime check. No new spread-history or matched-null capability was implemented: neither is supported by the current data or harness; fixed-spread simulations would misstate the gate.

## 2026-11-13 — Fedora-to-Mac EA synchronization

The canonical `mql5/Experts/Vilona/MT5BridgeEA.mq5` was synchronized from Fedora to the live Mac MT5 data directory over SSH/Tailscale using `scripts/sync_ea_to_mac.sh`. Local and remote MD5: `1234c7275f32ea3ab097ee647114b0d2`. The Mac source now uses per-symbol `SYMBOL_DIGITS` in `RatesToJson`; the prior deployed source used `_Digits` and was unsafe when switching symbols. The Mac MetaEditor log already contains a successful compile of the synchronized source (`0 errors, 0 warnings`, 1.552 s); the headless Wine invocation could not create a GUI window, so the terminal remains the authoritative loaded-artifact check. The running agent/EA health endpoint reports `ea=true`.

The bridge exposes only OHLCV candles and current spread/limits; it does not expose historical per-bar bid/ask spreads. No synthetic spread history was substituted.

## 2026-11-13 — Three-year verified-history fetch attempt

The Fedora fetcher attempted `scripts/fetch_verified_candles.py --timeframe 5m --days 1095 --symbols XAUUSD,EURUSD,USDJPY --out data/research_3y_m5 --url http://127.0.0.1:8765 --timeout 120`. Result: XAUUSD was rejected because the broker returned only `525.0` days; EURUSD was rejected because only `11` distinct close ticks appeared across `99,976` bars at tick size `0.00001`; USDJPY was rejected because `100.0%` of bars lacked the advertised three-decimal precision. During the long request the bridge also reset connections (`Connection reset by peer` / `Remote end closed connection`). No prices were scaled, repaired, or accepted as verified. Three-year strategy evidence remains unavailable.

## 2026-11-13 — Bridge outage after long history request

The Mac agent remained reachable, but its health changed to `ea=false` with `last_ea_poll_seconds_ago` above 1,000 seconds. The MCP `mt5.connect` response was explicit: `account.success=false`, `error=ea_timeout`, `detail=EA did not answer in time`. Therefore later local probes returning zero bars are not usable candle evidence. Mac terminal logs still show the synchronized EA source's prior successful compile (`0 errors, 0 warnings`), but no fresh runtime symbol-switch regression was claimed while the EA was detached/unresponsive.

## 2026-11-13 — EA redeployed; symbol-switch precision regression PASS

The operator compiled and attached the synchronized EA on the Mac: `ex5` timestamp `2026-11-13 01:03:25`, `61,126` bytes, MetaEditor log `0 errors, 0 warnings`. Agent health returned `ea=true`. The MCP bridge service (`vilona-mt5-mcp.service`) was restarted because its in-memory command queue still held a stale `account_info` request from the pre-compile EA session, which made `mt5.connect` report `ea_timeout` even though the EA was polling; after restart `connect=True`.

Live regression with the chart fixed on `EURUSD` and each symbol queried in turn:

| Symbol | Metadata digits | Tick bid | M5 close | Printed decimals |
| --- | --- | --- | --- | --- |
| EURUSD | 5 | 1.10545 | 1.10546 | 5 |
| USDJPY | 3 | 159.698 | 159.698 | 3 |
| XAUUSD | 2 | 3873.16 | 3873.16 | 2 |

Precision now follows the requested symbol instead of the chart, so the digits defect is fixed, not merely present in source. Note the broker metadata: EURUSD `point=0.00001`/`digits=5` and USDJPY `point=0.001`/`digits=3`, which is why the earlier feeds printing only two decimals were invalid rather than merely rounded.

## 2026-11-13 — Verified one-year M5 fetch and strategy result

After the EA digits correction, a 365-day fetch from the live broker succeeded for all three symbols: XAUUSD `68,909` bars, EURUSD `74,445`, USDJPY `74,476`. `scripts/candle_quality.py --dir data/research_1y_m5` accepted all sets: XAUUSD `50,382` distinct close ticks, EURUSD `8,130`, USDJPY `10,409`; all OHLC order shares `1.000`.

The 12-month `scalp_5m` backtest on this snapshot failed B9: 13,650 trades, expectancy `-$2.21`, PF `0.544`; OOS 6,760 trades, expectancy `-$4.06`, PF `0.502`. By symbol, OOS PF was XAUUSD `0.751`, EURUSD `0.264`, USDJPY `0.189`.

The first matched-null replay attempt was invalid: candidate P/L residual reached `$4.336` versus production. `scripts/matched_null_test.py` now fails closed rather than publishing a null verdict. Historical per-bar bid/ask spread is also unavailable, so the frozen three-year/spread-aware gate cannot pass with this snapshot. No post-OOS parameter changes were made.


## 2026-11-13 — operator-authorized breakout implementation

The operator explicitly authorized implementing the XAUUSD M15/1h breakout rule despite the recorded B9 failure (full-period PF `0.636`, OOS PF `0.705`). The implementation is bounded by the existing stale-tick, weekend, spread, daily-loss, drawdown, consecutive-loss, exposure, LLM, protected-SL, and kill-switch guards. This authorization does **not** change the Phase 4 research verdict or prove production readiness.

The scanner now supports `KINGAI_STRATEGY=breakout` with a 20-bar M15 range break aligned to 1h EMA21/EMA50. Focused tests: `53 passed`. No live order was sent during this implementation; the operator kill switch remains the final execution control until runtime verification is complete.

## 2026-11-17 — XAUUSD candidate recheck on verified 190-day data

On branch `improve/smart-bot`, the existing backtest tool was rerun without changing production strategy code or tuning against OOS results. All results use `data/candles_m5_verified`/`data/candles`, fixed configured costs, and the existing walk-forward split. Results were recorded in `vilona_v2.db` under `research:*` settings.

| Candidate | Full trades | Full PF | Full expectancy | OOS trades | OOS PF | OOS expectancy | Gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `scalp_5m` | 2,450 | 0.765 | -$3.13 | 1,245 | 0.758 | -$4.25 | FAIL |
| M15 `pullback` | 462 | 0.824 | -$3.71 | 238 | 0.738 | -$5.59 | FAIL |
| M15 `breakout` | 309 | 1.019 | +$0.40 | 156 | 0.956 | -$1.03 | FAIL |

The breakout candidate is closest but still fails the required PF > 1.1 and positive OOS expectancy. No candidate is promoted to the scanner or live execution. Paper mode and the Phase 4 block remain unchanged.
