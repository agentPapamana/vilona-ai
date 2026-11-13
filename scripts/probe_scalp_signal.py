"""Print the live scalp 15m/5m signal for one symbol, using real bridge candles.

Read-only: no order is sent, no DB row is written.

    PYTHONPATH=. python scripts/probe_scalp_signal.py --symbol XAUUSD
"""
from __future__ import annotations

import argparse
import sys

from config import get_config
from core.quant_scanner import compute_scalp_5m_signal
from trading.bridge_mt5_mcp import MT5MCPBridge


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--band", type=float, default=None,
                        help="override KINGAI_SCALP_PULLBACK_BAND for this probe")
    args = parser.parse_args()

    cfg = get_config()
    bridge = MT5MCPBridge(cfg.exchange.mt5_mcp_url, timeout=cfg.exchange.mt5_mcp_timeout)
    if not bridge.connect():
        print("bridge connect failed", file=sys.stderr)
        return 1

    c15m = bridge.get_candles(args.symbol, "15m", limit=60)
    c5m = bridge.get_candles(args.symbol, "5m", limit=60)
    tick = bridge.get_tick(args.symbol)
    band = args.band if args.band is not None else float(cfg.trading.scalp_pullback_band)

    action, confidence, reason, atr = compute_scalp_5m_signal(
        c15m, c5m, float(tick.bid), band
    )
    closes_15m = [float(row.get("close", 0.0)) for row in c15m]
    from core.quant_scanner import compute_ema
    ema21 = compute_ema(closes_15m, 21)
    ema50 = compute_ema(closes_15m, 50)

    print(f"symbol={args.symbol} bid={tick.bid:.5f} ask={tick.ask:.5f}")
    print(f"candles 15m={len(c15m)} 5m={len(c5m)} band={band}")
    print(f"15m EMA21={ema21:.5f} EMA50={ema50:.5f} ATR5m={atr:.5f}")
    print(f"signal={action} confidence={confidence:.2f} reason={reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
