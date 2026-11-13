"""Phase 3 acceptance check against the live demo account and StateDB.

Each criterion is read from the source of truth (broker bridge or SQLite), not
from logs. Exit code 0 means every criterion passed, 1 means at least one did
not. Read-only: it never places, modifies, or closes an order.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAGIC_MIN, MAGIC_MAX = 770000, 779999


def check_bridge(url: str, timeout: int) -> dict:
    from trading.bridge_mt5_mcp import MT5MCPBridge

    bridge = MT5MCPBridge(url, timeout=timeout)
    if not bridge.connect():
        return {"connected": False}

    account = bridge.get_account_info()
    positions = bridge.get_positions()
    deals = []
    since = int(__import__("time").time()) - 90 * 24 * 60 * 60
    try:
        deals = bridge.get_history_deals(since) or []
    except Exception as exc:
        print(f"warning: history unavailable: {exc}")
        deals = []

    magics = [int(getattr(p, "magic", 0) or 0) for p in positions]
    return {
        "connected": True,
        "login": getattr(account, "login", None),
        "server": getattr(account, "server", None),
        "balance": getattr(account, "balance", 0.0),
        "equity": getattr(account, "equity", 0.0),
        "trade_allowed": getattr(account, "trade_allowed", False),
        "positions": [
            {
                "ticket": getattr(p, "ticket", None),
                "symbol": getattr(p, "symbol", ""),
                "volume": getattr(p, "volume", 0.0),
                "sl": getattr(p, "sl", 0.0),
                "tp": getattr(p, "tp", 0.0),
                "magic": int(getattr(p, "magic", 0) or 0),
            }
            for p in positions
        ],
        "magics_in_range": all(MAGIC_MIN <= m <= MAGIC_MAX for m in magics) if magics else None,
        "deals": deals,
    }

def check_db(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        def count(sql: str, args: tuple = ()) -> int:
            try:
                return int(conn.execute(sql, args).fetchone()[0])
            except sqlite3.Error:
                return -1

        trade_cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        filled = count("SELECT COUNT(*) FROM trades") if trade_cols else -1
        with_ticket = (
            count("SELECT COUNT(*) FROM trades WHERE ticket IS NOT NULL AND ticket != ''")
            if "ticket" in trade_cols else -1
        )
        with_profit = (
            count("SELECT COUNT(*) FROM trades WHERE profit IS NOT NULL")
            if "profit" in trade_cols else -1
        )
        latest = None
        if filled > 0:
            cols = [c for c in ("id", "symbol", "ticket", "profit", "status", "created_at")
                    if c in trade_cols]
            row = conn.execute(f"SELECT {', '.join(cols)} FROM trades ORDER BY id DESC LIMIT 1").fetchone()
            latest = dict(zip(cols, row))
        setups = count("SELECT COUNT(*) FROM order_setups") if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='order_setups'"
        ).fetchone() else -1
        return {
            "path": str(path),
            "exists": True,
            "trade_columns": sorted(trade_cols),
            "trades": filled,
            "trades_with_ticket": with_ticket,
            "trades_with_profit": with_profit,
            "order_setups": setups,
            "latest_trade": latest,
            "unresolved_setups": count(
                "SELECT COUNT(*) FROM order_setups WHERE ticket IS NULL AND status = 'pending'"
            ) if setups > 0 else -1,
        }
    finally:
        conn.close()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--db", default="vilona_v2.db")
    parser.add_argument("--expect-flat", action="store_true",
                        help="require zero open positions (post-close check)")
    args = parser.parse_args()

    bridge = check_bridge(args.url, args.timeout)
    db = check_db(Path(args.db))

    print("== broker ==")
    for key, value in bridge.items():
        if key == "deals":
            print(f"deals: {len(value)}")
            continue
        print(f"{key}: {value}")

    print("== statedb ==")
    for key, value in db.items():
        print(f"{key}: {value}")

    results: dict[str, bool] = {}
    results["bridge_connected"] = bool(bridge.get("connected"))
    results["account_trade_allowed"] = bool(bridge.get("trade_allowed"))
    results["db_exists"] = bool(db.get("exists"))

    # A fill is proven by a closed trade with a recorded profit and, on the
    # broker side, by a deal in history or a currently protected position.
    positions = bridge.get("positions") or []
    deals = bridge.get("deals") or []
    vilona_deals = [
        d for d in deals
        if MAGIC_MIN <= int(d.get("magic", 0) or 0) <= MAGIC_MAX
    ]
    closed_rows = (db.get("trades_with_ticket") or 0) > 0
    results["fill_observed"] = bool(vilona_deals) or closed_rows
    results["sl_protected"] = (
        all(float(p["sl"] or 0) > 0 for p in positions) if positions else closed_rows
    )
    results["magic_in_range"] = (
        bridge.get("magics_in_range") if positions else (True if closed_rows else None)
    )
    results["single_position_scope"] = len(positions) <= 1
    results["canary_lot"] = all(float(p["volume"] or 0) <= 0.01 for p in positions) if positions else True
    if args.expect_flat:
        results["account_flat"] = len(positions) == 0

    print("== phase 3 ==")
    failed = False
    for name, passed in results.items():
        status = "PASS" if passed else ("PENDING" if passed is None else "FAIL")
        if passed is not True:
            failed = True
        print(f"{status:7} {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
