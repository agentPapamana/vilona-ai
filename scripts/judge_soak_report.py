#!/usr/bin/env python3
"""Paper-soak judge report.

Prints the per-symbol judge error rate and latency plus the readiness verdict, so
the decision to consider a canary is based on recorded evidence rather than
impression. Read-only: it never places or authorises a trade.

Usage:
    PYTHONPATH=. python scripts/judge_soak_report.py
    PYTHONPATH=. python scripts/judge_soak_report.py --model ag/gemini-3.8-flash-medium
"""
from __future__ import annotations

import sys
import argparse

from core.soak_readiness import evaluate_soak_readiness
from core.reporting import filter_judge_rows, summarize_judge_rows
from core.state_db import StateDB


def count_paper_setups(paper_orders: list[dict]) -> tuple[int, int]:
    """Count distinct durable setup IDs; return ``(distinct_setups, excluded_rows)``.

    ``orders.setup_key`` exists only once the idempotency code landed. Rows with
    a NULL key were written before it, so they cannot prove that the current
    code deduplicates anything. They are excluded from soak counting entirely
    rather than fingerprinted by (symbol, side, quantity, sl, tp), which let old
    evidence inflate the readiness gate.
    """
    keys = {o["setup_key"] for o in paper_orders if o.get("setup_key") is not None}
    excluded = sum(1 for o in paper_orders if o.get("setup_key") is None)
    return len(keys), excluded

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Filter stats by LLM model")
    parser.add_argument(
        "--since-hours", type=float, default=None,
        help="Only count orders created within the last N hours (excludes stale test rows)",
    )
    args = parser.parse_args()

    since = None
    if args.since_hours is not None:
        import time as _time
        since = _time.time() - (args.since_hours * 3600.0)

    db = StateDB()
    try:
        # Use one timestamped row set so the requested soak window applies to
        # verdict counts and per-symbol metrics as well as paper orders.
        decisions = filter_judge_rows(db.get_judge_decisions(limit=1000000), since)
        if args.model is not None:
            decisions = [row for row in decisions if row.get("model") == args.model]
        stats, metrics = summarize_judge_rows(decisions)
        paper_orders = [
            o for o in db.get_orders(limit=10000, since=since) if o.get("type") == "paper"
        ] if hasattr(db, "get_orders") else []
        paper_trades, legacy_rows = count_paper_setups(paper_orders)
        paper_rows = len(paper_orders) - legacy_rows
    finally:
        db.close()

    print("=== Judge decision totals ===")
    for key in ("total", "approved", "vetoed", "errors", "no_verdict"):
        print(f"  {key:12s} {stats.get(key, 0)}")

    print("\n=== Per symbol ===")
    if not metrics:
        print("  (no judge decisions recorded yet)")
    for symbol, m in metrics.items():
        print(
            f"  {symbol:8s} total={m['total']:<4d} approved={m['approved']:<3d} "
            f"vetoed={m['vetoed']:<3d} errors={m['errors']:<3d} "
            f"error_rate={m['error_rate']:.1%} avg_latency={m['avg_latency_ms']:.0f}ms"
        )

    readiness = evaluate_soak_readiness(stats, paper_trades=paper_trades, paper_rows=paper_rows)
    print(f"\n=== Soak readiness: {'READY' if readiness['ready'] else 'NOT READY'} ===")
    print(f"  distinct paper setups: {readiness['paper_trades']}")
    print(f"  paper rows (raw):      {readiness['paper_rows']}")
    print(f"  duplicate rows:        {readiness['duplicate_rows']} ({readiness['duplicate_share']:.1%})")
    print(
        f"  legacy rows excluded:  {legacy_rows} "
        "(setup_key IS NULL; written before idempotency, not counted)"
    )
    if readiness["blockers"]:
        print("  blockers:")
        for b in readiness["blockers"]:
            print(f"    - {b}")
    else:
        print("  no blockers; a canary may be proposed for human approval")

    return 0 if readiness["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
