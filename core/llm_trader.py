"""LLM Trader Gate — LLM only speaks when the deterministic rule says WAIT.

The trader runs after the rule-based scanner and before risk manager evaluation.
It must never override a BUY/SELL signal from the rule. Only WAIT triggers LLM.
All other cases (garbage, timeout, error) leave the decision at WAIT.
"""
from __future__ import annotations

import logging
from typing import Optional, Callable, Any

from agents import Signal
from agents.trader import parse_signal

logger = logging.getLogger(__name__)


def llm_trader_signal(
    llm_client: Callable[[str, str], Optional[str]],
    snapshot: Any,
    symbol: str,
    rule_action: str,
    enabled: bool = True,
) -> Optional[Signal]:
    """Ask the LLM trader for a signal if the rule yields WAIT.

    Returns:
        Signal or None.
        None means no signal — leave as WAIT.
    """
    if not enabled:
        return None

    if rule_action.upper() in ("BUY", "SELL"):
        # Deterministic signal takes precedence. Never override.
        return None

    try:
        # Build prompt for the LLM trader.
        # This is a simplified version; full implementation uses MarketSnapshot.
        user_prompt = f"Scan {symbol} and decide BUY/SELL/WAIT. Return JSON only."

        raw_response = llm_client("", user_prompt)
        if not raw_response:
            logger.warning(f"LLM trader returned empty response for {symbol}")
            return None

        signal = parse_signal(raw_response, symbol)
        if signal.action not in ("BUY", "SELL"):
            # WAIT, unparseable prose, or an unknown action means "no signal".
            logger.info(f"LLM trader gave no actionable signal for {symbol}: {signal.action}")
            return None

        logger.info(f"LLM trader signal for {symbol}: {signal.action} (confidence={signal.confidence})")
        return signal

    except Exception as e:
        logger.error(f"LLM trader call failed for {symbol}: {e}", exc_info=True)
        return None
