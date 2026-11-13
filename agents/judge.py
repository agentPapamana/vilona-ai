from __future__ import annotations
import json
from typing import Optional
from agents import Signal
from agents.risk_manager import RiskVerdict

SYSTEM_PROMPT = (
    "You are an independent judge reviewing a proposed trade. Look for "
    "logical flaws, overfitting to recent noise, or ignored risks. "
    "The required output language is English. Translate any non-English input "
    "or reasoning into concise, professional English before writing the reason. "
    "Return JSON only with exactly this shape: "
    "{\"verdict\": \"APPROVE\" or \"VETO\", \"reason\": \"English reason\"}. "
    "The verdict value must be exactly APPROVE or VETO in uppercase."
)

def build_user_prompt(
    signal: Signal,
    risk: RiskVerdict,
    research: Optional[dict] = None,
    snapshot=None,
    account: Optional[dict] = None,
) -> str:
    risk_data = {
        "approved": risk.approved,
        "lot_size": risk.lot_size,
        "sl_price": risk.sl_price,
        "tp_price": risk.tp_price,
        "rr_ratio": risk.rr_ratio,
        "reason": risk.reason,
    }
    equity = float((account or {}).get("equity") or 0.0)
    tick_value = float(getattr(snapshot, "tick_value", 0.0) or 0.0)
    tick_size = float(getattr(snapshot, "tick_size", 0.0) or 0.0)
    sl_distance = abs(float(signal.entry_hint or 0.0) - float(risk.sl_price or 0.0))
    if equity > 0 and tick_size > 0 and tick_value > 0:
        estimated_risk = (sl_distance / tick_size) * tick_value * float(risk.lot_size or 0.0)
        risk_data["risk_pct_of_equity"] = round(estimated_risk / equity * 100.0, 6)
    else:
        risk_data["risk_pct_of_equity"] = None

    market_context = None
    if snapshot is not None:
        market_context = {
            "timeframe": "15m/1h",
            "bid": snapshot.tick_bid,
            "ask": snapshot.tick_ask,
            "spread_pct": snapshot.spread_pct,
            "atr_15m": snapshot.atr_15m,
            "candles_15m_tail": snapshot.candles_15m[-20:],
            "candles_1h_tail": snapshot.candles_1h[-20:],
            "news_headlines": snapshot.news_headlines,
            "tick_value": snapshot.tick_value,
            "tick_size": snapshot.tick_size,
        }

    return json.dumps({
        "market_context": market_context,
        "account": account,
        "signal": {
            "symbol": signal.symbol,
            "action": signal.action,
            "confidence": signal.confidence,
            "reason": signal.reason,
            "entry_hint": signal.entry_hint,
            "sl_hint": signal.sl_hint,
            "tp_hint": signal.tp_hint,
        },
        "risk_verdict": risk_data,
        "research": research or {},
    }, default=str)

def parse_judge(raw: str) -> tuple[bool, str]:
    data = {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        s = raw.find("{"); e = raw.rfind("}")
        if s >= 0 and e > s:
            try:
                data = json.loads(raw[s:e + 1])
            except json.JSONDecodeError:
                data = {}
    # Fail-closed: when the LLM response is unparseable or omits an explicit
    # APPROVE verdict, veto the trade. Parsing garbage must never silently
    # approve an order.
    verdict = str(data.get("verdict", "")).upper() if data else ""
    reason = str(data.get("reason", ""))[:500] if data else "unparseable judge response - veto"
    # Only an explicit APPROVE is an approval. Anything else ("MAYBE", "VETO",
    # empty, unparseable) is a veto.
    reason = enforce_english(reason, verdict)
    return (verdict == "APPROVE", reason)


# Deterministic English fallback. The judge is instructed to answer in English,
# but models occasionally reply in another language; a machine-checkable guard
# runs Python-side so the audit trail stays consistent.
FALLBACK_REASON = {
    "approve": "Approved: judge returned a non-English reason; verdict preserved.",
    "veto": "Vetoed: judge returned a non-English reason; verdict preserved.",
    "unknown": "No verdict: judge returned a non-English or unparseable response.",
}


def _has_cjk(text: str) -> bool:
    return any(
        "\u3040" <= ch <= "\u30ff"  # Hiragana / Katakana
        or "\u4e00" <= ch <= "\u9fff"  # CJK Unified Ideographs
        or "\uac00" <= ch <= "\ud7af"  # Hangul
        for ch in text
    )


def _has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def _has_arabic(text: str) -> bool:
    return any("\u0600" <= ch <= "\u06ff" for ch in text)


def _has_thai(text: str) -> bool:
    return any("\u0e00" <= ch <= "\u0e7f" for ch in text)


def is_english(text: str) -> bool:
    """Heuristic: reject scripts that are definitely not English.

    Diacritic Latin languages (Vietnamese, French, etc.) are not rejected here
    because they share the Latin range; the model is instructed to answer in
    English and the common-language signal below handles Vietnamese tokens.
    """
    if not text or not text.strip():
        return True
    if _has_cjk(text) or _has_cyrillic(text) or _has_arabic(text) or _has_thai(text):
        return False
    return True


_VIETNAMESE_MARKERS = (
    "không", "dữ liệu", "không giao dịch", "xác minh", "tín hiệu",
    "giá", "rủi ro", "nến", "phân tích",
)


def enforce_english(reason: str, verdict: str = "") -> str:
    """Return the reason untouched if English-looking, else a stable fallback."""
    if not reason:
        return reason
    lowered = reason.lower()
    if any(marker in lowered for marker in _VIETNAMESE_MARKERS):
        text = FALLBACK_REASON.get(verdict.lower().strip(), FALLBACK_REASON["unknown"])
        return f"{text} Original (untranslated): {reason[:300]}"
    if not is_english(reason):
        text = FALLBACK_REASON.get(verdict.lower().strip(), FALLBACK_REASON["unknown"])
        return f"{text} Original (untranslated): {reason[:300]}"
    return reason
