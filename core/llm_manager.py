"""Synchronous LLM manager decisions with strict, bounded action parsing."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents.judge import build_user_prompt

logger = logging.getLogger(__name__)

ENTRY_SYSTEM_PROMPT = (
    "You manage a proposed trade. Review the signal, market, account and deterministic "
    "risk result. Return JSON only: {\"action\":\"APPROVE|VETO|ADJUST\","
    "\"reason\":\"short English reason\",\"lot_size\":number|null,"
    "\"sl_price\":number|null,\"tp_price\":number|null}. Use ADJUST only to reduce "
    "risk or improve protection. Never increase the supplied lot size or widen the stop."
)
POSITION_SYSTEM_PROMPT = (
    "Manage this existing open trade. Return JSON only: {\"action\":\"HOLD|MODIFY|CLOSE\","
    "\"reason\":\"short English reason\",\"sl_price\":number|null,\"tp_price\":number|null}. "
    "A deterministic ladder automatically ratchets profit stops (BE at 1 ATR, 25% lock at 50% TP, "
    "50% lock at 75% TP). "
    "YOUR TWO CORE RESPONSIBILITIES: "
    "1. DEFENSE (CUT LOSS SMALL): When gain_atr <= -0.8 or position.profit < 0, inspect recent_15m candles. "
    "If the trend structure has broken or momentum opposes the trade, choose CLOSE immediately to cut the "
    "loss small and keep it reasonable, OR choose MODIFY with sl_price tightened closer to current price. "
    "Never allow an invalidated position to drift all the way to its full initial stop loss. "
    "2. PROFIT HARVESTING: When progress_to_tp >= 0.5 and recent momentum is fading or reversing, choose CLOSE "
    "to bank sure profit. "
    "Rules for MODIFY: sl_price may only tighten toward current price (never widen/remove); "
    "extend tp_price only when momentum is strongly aligned and stop already protects entry. Otherwise HOLD."
)


def _request(client: Any, system: str, user: str, timeout_s: float) -> str | None:
    try:
        result = client.generate_signal(system, user)
        if hasattr(result, "__await__"):
            async def run():
                try:
                    return await asyncio.wait_for(result, timeout=timeout_s)
                finally:
                    closer = getattr(client, "close", None)
                    if callable(closer):
                        try:
                            await asyncio.wait_for(closer(), timeout=5)
                        except Exception:
                            logger.debug("LLM manager client close failed", exc_info=True)
            return asyncio.run(run())
        return result
    except Exception:
        logger.exception("LLM manager request failed")
        return None


def _parse(raw: str | None, actions: set[str]) -> dict | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        start, end = raw.find("{"), raw.rfind("}")
        try:
            data = json.loads(raw[start:end + 1]) if start >= 0 and end > start else None
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict) or str(data.get("action", "")).upper() not in actions:
        return None
    data["action"] = str(data["action"]).upper()
    data["reason"] = str(data.get("reason", ""))[:500]
    return data


def entry_decision(client: Any, signal: Any, risk: Any, snapshot: Any, account: dict, timeout_s: float) -> dict | None:
    raw = _request(client, ENTRY_SYSTEM_PROMPT, build_user_prompt(signal, risk, snapshot=snapshot, account=account), timeout_s)
    return _parse(raw, {"APPROVE", "VETO", "ADJUST"})


def position_decision(client: Any, position: Any, snapshot: Any, timeout_s: float) -> dict | None:
    entry = float(position.price_open or 0.0)
    tp = float(position.tp or 0.0)
    current = float(position.price_current or 0.0)
    tp_dist = abs(tp - entry) if tp else 0.0
    is_buy = position.type == 0 or str(position.type).lower() == "buy"
    gain = (current - entry) if is_buy else (entry - current)
    payload = {
        "market": {"symbol": snapshot.symbol, "bid": snapshot.tick_bid, "ask": snapshot.tick_ask,
                   "atr_15m": snapshot.atr_15m, "spread_pct": snapshot.spread_pct,
                   "recent_15m": [
                       {k: c.get(k) if isinstance(c, dict) else getattr(c, k, None) for k in ("open", "high", "low", "close")}
                       for c in (getattr(snapshot, "candles_15m", []) or [])[-5:]
                   ]},
        "position": {"ticket": position.ticket, "symbol": position.symbol,
                     "side": "BUY" if is_buy else "SELL",
                     "volume": position.volume, "entry": position.price_open,
                     "current": position.price_current, "sl": position.sl, "tp": position.tp,
                     "profit": position.profit,
                     "gain_atr": round(gain / float(snapshot.atr_15m), 2) if getattr(snapshot, "atr_15m", 0) and float(snapshot.atr_15m) > 0 else 0.0,
                     "progress_to_tp": round(gain / tp_dist, 3) if tp_dist > 0 else None},
    }
    return _parse(_request(client, POSITION_SYSTEM_PROMPT, json.dumps(payload), timeout_s), {"HOLD", "MODIFY", "CLOSE"})
