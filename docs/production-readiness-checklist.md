# Vilona AI v2 — Production Readiness Checklist

## Decision

**Status: `NOT READY` for real-account trading.**

PAPER mode is the approved operating mode. Protected-order demo E2E has passed. The demo canary and real-account gates remain incomplete.

See [`operating-flow.md`](operating-flow.md) for the runtime flow and rollback procedure. See [`paper-trading-audit.md`](paper-trading-audit.md) for the PAPER contract and audit schema.

## Gate A — PAPER soak and manager stability

- [ ] Record at least `50` fresh manager decisions.
- [ ] Record at least `50` distinct paper setups.
- [ ] Keep infrastructure/action error rate at or below `10%`.
- [ ] Show a valid nonzero approval share; `ERROR` is not a substantive `VETO`.
- [ ] Confirm no duplicate setup inflation after repeated scans or restart.
- [ ] Confirm manager adjustments never increase lot size or widen the deterministic stop.
- [ ] Confirm `judge_soak_report.py` returns `READY`.

Historical evidence remains insufficient: the last recorded sample was `15` decisions with `0 APPROVE`, `6 VETO`, and `9 ERROR` (`60%` errors). This is a blocker, not a production baseline.

Command:

```bash
PYTHONPATH=. /tmp/vilvenv/bin/python scripts/judge_soak_report.py --since-hours 24
```

## Gate B — Demo bridge and controlled canary

Completed:

- [x] Verify demo account identity and bridge connectivity.
- [x] Run protected-order E2E at minimum volume.
- [x] Confirm SL verification and cleanup.

Latest recorded protected-order E2E:

- Account: `MetaQuotes-Demo`, login `5058637180`.
- Symbol/action: XAUUSD BUY `0.01`.
- Ticket: `59291108467`.
- SL: `3993.37`; TP: `4033.37`; magic: `770714`.
- Broker confirmed SL; cleanup returned `open_positions=0`.

Still required:

- [ ] Run a one-symbol canary only when a real qualifying setup appears.
- [ ] Keep one position maximum and minimum volume.
- [ ] Manually monitor fill, SL, TP, manager decision, bridge health, and cleanup.
- [ ] Verify any manager `MODIFY` tightens SL and any `CLOSE` targets only Vilona-owned magic.
- [ ] Abort on SL verification, bridge health, daily-loss, or cleanup failure.

Do not force a synthetic signal to claim canary completion.

## Gate C — Real-account readiness

- [ ] Gates A and B complete with durable evidence.
- [ ] Compare deterministic baseline with paper/canary results: expectancy, profit factor, win rate, and maximum drawdown.
- [ ] Exercise the file kill switch and verify new entries stop.
- [ ] Exercise daily-loss shutdown and verify new entries stop.
- [ ] Verify restart/reconnect does not orphan Vilona positions or confuse manual positions.
- [ ] Verify rollback to PAPER mode.
- [ ] Obtain explicit human approval for account credentials and risk limits.

Real-account trading stays disabled until every item is evidenced.

## Operating configuration

Default safe configuration:

```env
vilona_EXCHANGE=mt5
vilona_MT5_TRANSPORT=mcp
vilona_PERSONAS=trader,risk_manager,judge
vilona_LLM_ENABLED=true
vilona_PAPER_TRADING=true
```

Manager-disabled PAPER comparison:

```env
vilona_LLM_ENABLED=false
vilona_PAPER_TRADING=true
```

`vilona_LLM_ASYNC_REVIEW` is removed and must not be configured. `.env` changes require a server restart.

## Promotion and rollback procedure

1. Run the soak report and save its output with the database time window.
2. Keep PAPER mode until Gate A is complete.
3. For Gate B, set `vilona_PAPER_TRADING=false` only on the approved demo account, use one symbol, minimum volume, and one-position maximum.
4. Monitor the canary manually. Stop immediately on any protection, bridge, risk-lock, or cleanup failure.
5. Roll back by setting `vilona_PAPER_TRADING=true` and restarting through `scripts/run_app_server.sh`.
6. Verify the active environment:

```bash
pid=$(pgrep -o -f '[a]pp_server.py')
tr '\0' '\n' </proc/$pid/environ | grep -E '^vilona_(EXCHANGE|MT5_TRANSPORT|LLM_ENABLED|PAPER_TRADING|PERSONAS)='
```

7. Confirm no unexpected open positions remain and the kill switch is available.

## Automated verification

Current repository verification:

```bash
PYTHONPATH=. /tmp/vilvenv/bin/python -m pytest -q
# 224 passed, 3 skipped
```

This is a code-quality signal only. It does not clear the PAPER soak, demo canary, or human approval gates.
