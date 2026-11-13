"""Read-only daily-risk probe. It never submits or alters orders/positions."""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from config import Config
from trading.bridge_mt5_mcp import MT5MCPBridge
from core.risk_lock import daily_pl_from_bridge_day


def main() -> int:
    cfg = Config.from_env()
    bridge = MT5MCPBridge(
        mcp_url=cfg.exchange.mt5_mcp_url,
        timeout=cfg.exchange.mt5_mcp_timeout,
    )
    ok = bridge.connect()
    message = "connected" if ok else "MCP bridge connection failed"
    print(json.dumps({"connected": ok, "message": message}))
    if not ok:
        return 2
    try:
        now = time.time()
        day_start = bridge.get_server_day_start(now)
        records = bridge.get_history_deals(day_start) if day_start is not None else None
        positions = bridge.get_positions() or []
        account = bridge.get_account_info()
        print(json.dumps({
            "login": getattr(account, "login", None), "server": getattr(account, "server", None),
            "equity": getattr(account, "equity", None),
            "host_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "day_start_epoch": day_start,
            "day_start_utc": datetime.fromtimestamp(day_start, timezone.utc).isoformat() if day_start else None,
            "history_supported": records is not None,
            "closed_deal_count": len(records) if records is not None else None,
            "deals": records,
            "realized_pl": daily_pl_from_bridge_day(bridge, now_epoch=now),
            "open_positions": len(positions),
            "floating_pl": sum(float(getattr(p, "profit", 0) or 0) + float(getattr(p, "swap", 0) or 0) for p in positions),
        }, indent=2, default=str))
        return 0 if day_start is not None and records is not None and account is not None else 3
    finally:
        bridge.disconnect()

if __name__ == "__main__":
    raise SystemExit(main())
