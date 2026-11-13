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
    "You are a conservative, fast trade risk manager. Decide only from the supplied live snapshot; do not invent market facts. "
    "Return one compact JSON object only: {\"action\":\"HOLD|MODIFY|CLOSE\",\"reason\":\"short reason\","
    "\"sl_price\":number|null,\"tp_price\":number|null}. Make one decisive recommendation. "
    "DEFENSE has priority: if gain_atr <= -0.8, inspect recent_15m structure and decide whether the thesis is invalidated. "
    "CLOSE only when evidence supports invalidation or loss containment; otherwise MODIFY only to a valid tighter stop. "
    "If position.profit is negative but gain_atr > -0.8 and structure is intact/unclear, HOLD; never close solely because profit is negative. "
    "PROTECT gains when progress_to_tp >= 0.5 and momentum is fading/reversing: CLOSE to bank or MODIFY to tighten SL. "
    "Do not make routine changes. A deterministic_plan.trend_tp_candidate is already applied as the fast default; "
    "only override it with clear contrary evidence. MODIFY may only tighten SL toward price; never widen/remove SL. "
    "Extend TP only with strongly aligned momentum and an SL already protecting entry; otherwise keep TP unchanged. "
    "Use HOLD whenever evidence is insufficient."
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
    atr = float(getattr(snapshot, "atr_15m", 0.0) or 0.0)
    gain_atr = gain / atr if atr > 0 else 0.0
    progress = gain / tp_dist if tp_dist > 0 else None
    sl = float(position.sl or 0.0)
    risk_free = sl > 0 and ((sl >= entry) if is_buy else (sl <= entry))
    payload = {
        "market": {"symbol": snapshot.symbol, "bid": snapshot.tick_bid, "ask": snapshot.tick_ask,
                   "atr_15m": atr, "spread_pct": snapshot.spread_pct,
                   "recent_15m": [
                       {k: c.get(k) if isinstance(c, dict) else getattr(c, k, None) for k in ("open", "high", "low", "close")}
                       for c in (getattr(snapshot, "candles_15m", []) or [])[-5:]
                   ]},
        "position": {"ticket": position.ticket, "symbol": position.symbol,
                     "side": "BUY" if is_buy else "SELL",
                     "volume": position.volume, "entry": position.price_open,
                     "current": position.price_current, "sl": sl, "tp": position.tp,
                     "profit": position.profit, "gain_atr": round(gain_atr, 2),
                     "progress_to_tp": round(progress, 3) if progress is not None else None,
                     "sl_protects_entry": risk_free},
        "deterministic_plan": {
            # The logic layer already extended the target this cycle; the manager
            # confirms or vetoes it rather than inventing an unrelated level.
            "trend_tp_candidate": getattr(snapshot, "trend_tp_candidate", None),
        },
    }
    return _parse(_request(client, POSITION_SYSTEM_PROMPT, json.dumps(payload), timeout_s), {"HOLD", "MODIFY", "CLOSE"})
