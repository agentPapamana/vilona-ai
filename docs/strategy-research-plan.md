# Three-week bounded strategy research decision

**Decision date:** 2026-11-13  
**Research window:** 2026-11-13 through 2026-12-04 (21 calendar days).  
**Operating mode:** PAPER only.  
**Research status:** permitted as a time-boxed investigation; Phase 4 remains BLOCKED. This plan does not authorize a canary, demo orders, or real-account activity.

## Why time-box it

Tested EMA/ATR intraday variants fail after estimated costs; the existing XAUUSD `scalp_5m` screen reported PF `0.576`, while a 30-run random-entry diagnostic had median PF `0.771`. The 30-run null sample is too small for inference. EURUSD/USDJPY histories are also invalid after the digits-formatting defect. A limited, pre-registered data repair and three distinct hypotheses are reasonable; indefinite variation inside the failed EMA/ATR family is not.

The project is not being stopped. Safety, ownership, backup, guard, and drill work remains useful independently of strategy discovery. No Phase 4 promotion follows from merely conducting this research.

## Pre-registered gate — freeze before new data/results

A candidate is eligible for review only if all conditions pass on fresh broker-verified data and realistic per-bar costs:

1. At least **three years** of usable OHLC history per enabled symbol; at least two independent symbols.
2. At least **30 closed OOS trades per enabled symbol** in each of at least three chronological, non-overlapping forward windows. Aggregate across windows only after reporting each window.
3. Positive expectancy and profit factor **> 1.1** on full history and every OOS window, after spread, commission, and swap where available.
4. OOS profit factor exceeds the **95th percentile** of at least **500 matched null runs per symbol**. Null runs must use the same timestamps, eligible trade count, direction-randomization rule, exits, sizing, costs, and locked windows as the candidate. Compare the candidate with each run using PF; no after-the-fact choice of null specification.
5. One final untouched holdout window, not used for selection, has positive expectancy, PF > 1.1, at least 30 trades per symbol, and beats the 95th percentile of the correspondingly matched null distribution. Holdout may be opened once, after candidate and parameters are frozen and recorded in the evidence log.
6. Costs use observed historical per-bar bid/ask spread and commissions. Missing spread history, precision errors, or unmodeled cost prevents a pass; fixed spread is diagnostic only.
7. No parameter changes, symbol exclusions, window shifts, or threshold relaxation after viewing OOS/holdout outcomes. Any change restarts with a new untouched holdout and a new log entry.

A gate pass means **candidate for further PAPER validation only**. It does not open Phase 4, enable orders, or grant real-account approval. The existing separate Phase 4 and Phase 5 safety gates remain mandatory. The production kill rule (PF < 1.0 over 30 minimum-lot trades) is not a substitute for the stricter research gate.

## Work schedule and scope ceiling

### Day 1 — safety, data repair, and preregistration

- **Completed:** stop the live-demo scanner; arm `/tmp/vilona-STOP_ENTRIES` during stop; confirm the MetaQuotes-Demo account has no open positions; set `vilona_PAPER_TRADING=true` in `.env`; restart and verify the process environment and startup log say `paper_trading=True`. This is the recorded canary rollback decision. A demo fill is no longer needed as evidence for strategy edge.
- Compile/redeploy `MT5BridgeEA.mq5` using per-requested-symbol `SYMBOL_DIGITS`; perform the symbol-switch regression: query EURUSD, USDJPY, then XAUUSD while the chart remains XAUUSD; independently compare JSON OHLC digits and tick alignment with broker metadata.
- Fetch fresh EURUSD and USDJPY M5/M15 history. Do not repair precision by scaling rounded prices.
- Freeze this plan and gate in version control before viewing the newly fetched strategy results. If the EA terminal/compiler is unavailable, document the exact missing dependency; no surrogate pass is permitted.

### Week 1 — data and cheapest diagnostic

- Pursue at least three years of verified OHLC plus historical per-bar bid/ask spread and broker commissions. If unavailable, record the limitation and do not substitute fixed spread as evidence.
- Run at least 500 matched null simulations per usable symbol with fixed seeds recorded in the output. The current `null_signal_test.py` is an unconditioned random-entry diagnostic with a fixed spread and a fixed seed; it does not match candidate timestamps or eligible trade counts, and it cannot satisfy gate criterion 4. Treat results as context only, never as the required null test.
- Run `scripts/cost_volatility_test.py` for the existing signal and record all buckets. The XAUUSD-only diagnostic on the 190-day, fixed-spread snapshot reported: all 2,214 trades, expectancy `-$3.38`, PF `0.736`; the declared low-cost bucket `[0.10, 0.15)` had 224 trades, expectancy `-$7.61`, PF `0.484`; the next bucket held only 14 trades. The production backtest on the same candles independently reported 2,214 trades and PF `0.736`, and its B9 gate failed. So the screen shows **no** “cost alone” explanation. It remains diagnostic only: fixed spread, 190 days, and one valid symbol fail the gate. A regression test now pins trade-level parity with the production backtest.
- No strategy-family variation is authorized during this first diagnostic.

### Weeks 2–3 — at most three new hypotheses

Test only the following, in order, using training windows and the frozen costs/null protocol:

1. H4 trend rule with trailing exit.
2. Session-range breakout.
3. Regime-filtered mean reversion.

One locked specification per hypothesis. Record failed attempts, parameters, timestamp range, data hash, code revision, costs, trades, PF, expectancy, drawdown, windows, and null percentile. Do not add a fourth hypothesis inside this window. No holdout is opened until a candidate survives training and rolling OOS checks and is frozen in the log.

## Stop/decision rule — 2026-12-04

If no candidate passes every pre-registered criterion by the end of the 21-day window, stop automated strategy discovery on this scope. Choose and record one of:

- retain Vilona as an execution, risk-management, monitoring, and audit layer for signals from an independently validated source; or
- stop the automated-trading portion while preserving safety and operational components.

No extension, new strategy family, or loosened threshold without a new explicit decision, new time box, and new untouched holdout. This rule is a decision checkpoint, not a precommitment to shut down the whole project today.

## Hard exclusions

- Do not open Phase 4 “to measure edge.”
- Do not lower PF/trade-count/null-percentile thresholds.
- Do not inspect the final holdout before parameters and candidate are frozen.
- Do not activate a live demo canary for more trades; current `.env` PAPER setting is required.
- Never enable a real account, regardless of research outcome, before all independent Phase 4 and Phase 5 gates pass and an operator approves.
- Do not port an unproven candidate to `core/quant_scanner.py`.

## Evidence log

See [`canary-evidence-log.md`](canary-evidence-log.md) for PAPER rollback, observed process/account state, the first diagnostic, and outstanding prerequisites. Update it for every experiment; do not overwrite failed results.
