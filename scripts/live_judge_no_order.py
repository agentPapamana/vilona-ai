"""Level-1 live validation: real MT5 data, real judge, NO order submission.

Drives the production evaluate_symbol against the live MT5 MCP bridge while a
guard bridge wraps every mutating call and refuses it. If a mutating call is
ever reached, the guard records it and the run fails — proving the judge gate
holds on live data.

Run:
  set -a; . ./.env; set +a; PYTHONPATH=. python scripts/live_judge_no_order.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from core.quant_scanner import QuantScanner
from trading.bridge_mt5_mcp import MT5MCPBridge

MUTATORS = (
    "send_market_order",
    "modify_position_sltp",
    "close_position",
    "send_pending_order",
    "cancel_pending_order",
)


class ReadOnlyGuard:
    """Passes reads through, blocks every mutating call."""

    def __init__(self, inner):
        self._inner = inner
        self.blocked: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in MUTATORS:
            def blocked(*a, **kw):
                self.blocked.append((name, {"args": a, "kwargs": kw}))
                raise RuntimeError(f"BLOCKED mutating call {name} during level-1 validation")
            return blocked
        return attr


def main() -> int:
    cfg = Config.from_env()
    print(f"[lvl1] llm.enabled={cfg.llm.enabled} model={cfg.llm.model}")
    print(f"[lvl1] paper_trading={cfg.paper_trading} symbols={cfg.trading.symbols}")

    inner = MT5MCPBridge(mcp_url="http://127.0.0.1:8765", timeout=30)
    if not inner.connect():
        print("[lvl1] FAIL: MT5 MCP connect failed")
        return 2

    guard = ReadOnlyGuard(inner)
    acc = inner.get_account_info()
    print(f"[lvl1] account equity={acc.equity} free_margin={acc.free_margin} "
          f"balance={acc.balance} server={acc.server}")

    events: list[str] = []

    def feed(message: str):
        events.append(f"FEED {message}")
        print(f"  {message}")

    def agent_feed(role: str, symbol: str, message: str):
        events.append(f"{role}:{symbol} {message}")
        print(f"  [{role}] {message}")

    sc = QuantScanner(bridge=guard, cfg=cfg, dashboard=None)
    sc._feed = feed
    sc._agent_feed = agent_feed

    t0 = time.time()
    for sym in cfg.trading.symbols:
        print(f"\n[lvl1] === {sym} ===")
        try:
            sc.evaluate_symbol(sym)
        except RuntimeError as e:
            print(f"  STOP: {e}")
            break
    dt = time.time() - t0

    judge_events = [e for e in events if "judge" in e.lower()]
    print(f"\n[lvl1] elapsed={dt:.2f}s judge_events={len(judge_events)}")
    for e in judge_events:
        print(f"  -> {e}")

    if guard.blocked:
        print(f"[lvl1] FAIL: mutating calls were attempted: {guard.blocked}")
        return 1

    print("[lvl1] PASS: no order submission attempted on live data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
