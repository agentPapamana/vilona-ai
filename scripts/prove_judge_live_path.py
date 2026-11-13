"""Live-path proof: run the REAL evaluate_symbol through judge wiring.

Uses a fake bridge that forces a deterministic BUY, real config object with
llm.enabled=True, and the real LLMClient pointed at the .env endpoint.
Nothing is sent to MT5: every bridge mutator is a stub.

Run:
  set -a; . ./.env; set +a; PYTHONPATH=. python scripts/prove_judge_live_path.py
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, TradingConfig
from core.quant_scanner import QuantScanner


@dataclass
class FakeTick:
    symbol: str
    bid: float
    ask: float
    timestamp_ms: int = 1700000000000


@dataclass
class FakeResult:
    success: bool = True
    message: str = "ok"
    ticket: int = 0


class ForcedBuyBridge:
    """Candles shaped so the EMA rule emits BUY. No MT5 calls."""

    def __init__(self):
        self.orders: list[dict] = []
        self._ticket = 111

    def get_positions(self, symbol=None):
        return []

    def get_tick(self, symbol):
        # bid slightly above EMA21 of the synthetic uptrend, inside 1.2*ATR
        return FakeTick(symbol, 1.1010, 1.1012)

    def get_candles(self, symbol, timeframe, limit=100):
        rows = []
        for i in range(limit):
            base = 1.0880 + i * 0.00018
            rows.append({
                "time": 1700000000 + i * 900,
                "open": base,
                "high": base + 0.00060,
                "low": base - 0.00060,
                "close": base + 0.00052,
                "volume": 10.0,
            })
        rows[-1]["close"] = rows[-1]["open"] + 0.00002
        return rows

    def get_symbol_info(self, symbol):
        from trading.bridge_base import SymbolInfo
        return SymbolInfo(
            symbol=symbol, point=0.00001, digits=5,
            lot_step=0.01, lot_min=0.01, lot_max=100.0,
            tick_value=1.0, tick_size=0.00001,
        )

    def get_account_info(self):
        return SimpleNamespace(equity=100000.0, free_margin=100000.0)

    def send_market_order(self, **kw):
        # Must never be called on a VETO. Record it if it is.
        self.orders.append(kw)
        self._ticket += 1
        return FakeResult(success=True, ticket=self._ticket)

    def modify_position_sltp(self, *a, **kw):
        return FakeResult()


def main() -> int:
    cfg = Config.from_env()
    print(f"[probe] llm.enabled={cfg.llm.enabled} model={cfg.llm.model} base={cfg.llm.base_url}")
    if not cfg.llm.enabled:
        print("[probe] REFUSING: KINGAI_LLM_ENABLED is not true; judge path would be skipped")
        return 2

    # Symbols from .env include XAUUSD; force EURUSD-shaped candles for a clean BUY.
    cfg.trading = TradingConfig(
        symbols=["EURUSD"],
        max_concurrent_positions=3,
        min_confidence=0.0,
        symbol_cooldown_seconds=0,
    )

    bridge = ForcedBuyBridge()
    events: list[str] = []
    sc = QuantScanner(bridge=bridge, cfg=cfg, dashboard=None)
    def feed(message: str):
        events.append(f"FEED {message}")
        print(f"  {message}")

    def agent_feed(role: str, symbol: str, message: str):
        events.append(f"{role} {message}")
        print(f"  [{role}] {message}")

    sc._feed = feed
    sc._agent_feed = agent_feed

    t0 = time.time()
    sc.evaluate_symbol("EURUSD")
    dt = time.time() - t0

    judge_lines = [e for e in events if "judge" in e.lower()]
    print(f"\n[probe] elapsed={dt:.2f}s orders_placed={len(bridge.orders)}")
    print(f"[probe] judge_events={len(judge_lines)}")

    ok = True
    if not judge_lines:
        print("[probe] FAIL: judge never spoke — LLM path not reached")
        ok = False
    else:
        for line in judge_lines:
            print(f"  -> {line}")

    vetoed = any("VETO" in e.upper() for e in judge_lines)
    if vetoed and bridge.orders:
        print("[probe] FAIL: vetoed but an order was still placed")
        ok = False
    if vetoed and not bridge.orders:
        print("[probe] PASS: judge vetoed, no order sent")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
