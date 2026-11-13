# Strategy research and Phase 4 go/no-go

**Status: Phase 4 BLOCKED. No tested strategy passes. Automatic order submission is halted and the scanner runs in PAPER until a candidate clears the gate below.** This file records evidence; the binding, time-boxed decision is [`strategy-research-plan.md`](strategy-research-plan.md) with the append-only [`canary-evidence-log.md`](canary-evidence-log.md).

## Pass criteria

A strategy is promotable only when the existing B9 gate and the stricter pre-registered research criteria in [`strategy-research-plan.md`](strategy-research-plan.md) pass on broker-verified data:

- At least three years of usable OHLC per enabled symbol, across at least two independent symbols.
- Full-period and every forward-window expectancy > 0 and profit factor > 1.1, after spread, commission, and swap.
- At least 30 OOS trades per enabled symbol in each of at least three chronological OOS windows.
- OOS profit factor above the 95th percentile of 500+ matched random-entry null runs per symbol.
- One untouched final holdout window passes the same test once, after candidate and parameters are frozen.
- Broker tick sizes and printed precision validated; costs include observed historical bid/ask spread, not a fixed substitute.
- No parameter may be selected by optimizing an OOS or holdout period. Any adjustment restarts with a new untouched holdout.

These are measurable acceptance criteria, not a promise that a passing strategy will be profitable live. A passing backtest is a candidate for PAPER validation only; Phase 4 and Phase 5 gates remain independent and mandatory.

## Evidence (2026-11-12)

| Rule / data | Result | Decision |
|---|---:|---|
| M15 pullback, all 3 configured symbols, 190 days | 1,077 trades; expectancy `-$8.88`; PF `0.587`; OOS expectancy `-$10.39`; OOS PF `0.575` | Fail |
| M15 breakout, all 3 configured symbols, 190 days | 620 trades; expectancy `-$8.54`; PF `0.636`; OOS expectancy `-$7.24`; OOS PF `0.705` | Fail |
| `scalp_5m`, M5, original unverified set | 4,642 trades; expectancy `-$4.62`; PF `0.533`; OOS 2,368 trades, expectancy `-$6.72`, PF `0.567` | Fail; EURUSD data invalid |
| `scalp_5m`, verified XAUUSD + USDJPY, M5, 190 days | 4,514 trades; expectancy `-$3.89`; PF `0.576`; OOS 2,318 trades, expectancy `-$6.08`, PF `0.594` | Fail |
| M15 breakout, XAUUSD only, commission set to `$0` (diagnostic) | OOS expectancy `+$1.95`; OOS PF `1.088` | Fail; not costed and PF below 1.1 |

Verified `scalp_5m` row command:

```text
.venv/bin/python scripts/backtest_strategy.py --months 6 --candles-dir-m5 data/candles_m5_verified --strategy scalp_5m --walk-forward --symbols XAUUSD,USDJPY
```

The remaining rows use `--candles-dir data/candles` (M15) with the same `--months 6 --walk-forward`; the breakout row adds `--symbols XAUUSD,USDJPY`, and the diagnostic row adds `--symbols XAUUSD --commission 0`.

The gate requires both the full-period verdict and the walk-forward-OOS verdict. The `$0`-commission breakout row passes the OOS part but fails the full-period part; the costed breakout row fails both. No row passes, so `core/quant_scanner.py` is intentionally unchanged: there is no proven rule to port.

### Data-quality finding

The legacy M5 EURUSD snapshot has 39,155 bars, but prices contain only eight distinct closes at two decimal places, while broker metadata reports a tick size of `0.00001`. A fresh range request to the local MT5 bridge returned the same quantized prices. That symbol cannot support a meaningful strategy test.

**Root cause found:** MQL5 `RatesToJson()` formatted prices using the chart-global `_Digits`, not the queried symbol’s digits. Thus bars for a 5-digit EURUSD queried from a 2-digit XAUUSD chart were rounded to two decimals. `mql5/Experts/Vilona/MT5BridgeEA.mq5` now obtains `SYMBOL_DIGITS` for each requested symbol before formatting OHLC. This source fix is not yet deployed/compiled on the MT5 terminal, so it is not yet evidence that the live bridge now returns correct EURUSD values.

`scripts/candle_quality.py` validates OHLC ordering, tick-grid alignment, distinct close ticks, and printed precision against broker tick metadata. All candle fetch paths apply that guard; the backtest also rejects invalid legacy snapshots rather than silently reusing them. Current verified M5 XAUUSD passes (35,859 bars; 27,215 distinct close ticks); M5 and M15 USDJPY fail because all bars print only two decimals although metadata claims three digits. M15 EURUSD also fails (13,053 bars, only 8 close ticks at `0.00001`).

Rebuild/redeploy the EA, restart the bridge, then refetch M5/M15 histories and rerun `scripts/candle_quality.py --dir <snapshot-dir>` before using those instruments in research. Do not multiply rounded prices to synthesize missing precision.

### Loss diagnosis

- A costed XAUUSD M5 pullback diagnostic with the production confidence filter removed still fails OOS (PF `0.802`, expectancy `-$1.27`). No-cost OOS is also negative (`-$0.41`); costs worsen a structurally weak signal, but removing cost alone does not create a pass.
- On the same diagnostic, only 14.0% of OOS pullback entries reach +1 ATR favorable excursion before the stop; median MFE is 0.45 ATR while the live target is 3 ATR. This screen omits the production confidence filter, so treat it as a directional hypothesis, not proof for changing production settings.
- Tested candidates (Donchian 20/55, EMA21/50 cross, reversal, wick rejection, 10-bar timeout, target 1.5 ATR, wider stop) did not produce a positive OOS expectancy/PF. None is eligible for live code.
- USDJPY's verified production `scalp_5m` OOS PF was previously reported as `0.329`; because the M5 feed now fails the stricter precision validator, that result must be considered unverified until the bridge is fixed and fresh data collected.

## Recommendation follow-up (2026-11-13)

- `scripts/null_signal_test.py` adds deterministic-seed random-side entries using the same ATR sizing, spread and commission assumptions as the backtest. A 30-run XAUUSD diagnostic produced median PF `0.771` (p05 `0.420`, p95 `1.124`) and median expectancy `-$5.65`; this small sample is not a calibration result. Run at least 200 simulations per symbol before interpreting the null distribution.
- MT5 `MqlRates` has OHLC and tick_volume, but no historical spread. Historical spread remains an explicit blocker; fixed spread is not an adequate substitute for session/rollover spread behavior.
- Current data covers 190 days, not 3+ years. A verified longer-history feed has not been obtained. Rolling 60/30 windows, multiple-testing adjustment, and exit-grid sweep remain outstanding.
- No live strategy or risk setting changed. No candidate has passed; Phase 4 remains blocked.

## Next work required for a pass

1. Compile and deploy the EA price-formatting correction; refetch and independently verify EURUSD and USDJPY precision against live broker metadata.
2. Obtain at least three years of verified candles and historical spreads, or record that no-go limitation; do not claim six-month histories cover regime risk.
3. Add rolling 60/30 walk-forward windows, an exit-parameter grid with a multiple-testing correction, and sufficiently large random-entry null distributions.
4. Test a genuinely different mechanism (for example regime-filtered mean reversion or session/structure entries) on training data only; freeze parameters before any holdout test.
5. Only after all existing criteria pass, port the exact tested rule to `core/quant_scanner.py`, add parity coverage, and re-run safety and phase checks. Until then Phase 4 stays blocked.
