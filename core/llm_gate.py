"""LLM Judge Gate — fail-closed veto layer for trading decisions."""
from __future__ import annotations

import asyncio
import logging
from typing import Tuple

from agents import Signal
from agents.risk_manager import RiskVerdict
from agents.judge import SYSTEM_PROMPT, build_user_prompt, parse_judge

logger = logging.getLogger(__name__)


def llm_judge_verdict(
    llm_client,
    signal: Signal,
    risk: RiskVerdict,
    enabled: bool = True,
    timeout_s: float = 30.0,
    snapshot=None,
    account: dict | None = None,
) -> Tuple[bool, str]:
    """Ask the LLM judge; only explicit APPROVE can pass."""
    if not enabled:
        return True, "LLM judge disabled"

    try:
        user_prompt = build_user_prompt(signal, risk, snapshot=snapshot, account=account)
        logger.debug("Judge prompt: %s...", user_prompt[:200])
        logger.info(
            "Judge request %s: prompt_chars=%d timeout_s=%.1f",
            getattr(signal, "symbol", "?"), len(user_prompt), timeout_s,
        )

        if callable(llm_client) and not hasattr(llm_client, "generate_signal"):
            raw_response = llm_client(SYSTEM_PROMPT, user_prompt)
        else:
            request = llm_client.generate_signal(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
            if not hasattr(request, "__await__"):
                raw_response = request
            else:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    closer = getattr(llm_client, "close", None)

                    async def _run_and_close():
                        try:
                            return await asyncio.wait_for(request, timeout=timeout_s)
                        finally:
                            if callable(closer):
                                try:
                                    await asyncio.wait_for(closer(), timeout=5)
                                except Exception:
                                    logger.debug("LLM client close failed", exc_info=True)

                    raw_response = asyncio.run(_run_and_close())
                else:
                    request.close() if hasattr(request, "close") else None
                    return False, "LLM judge unavailable inside running event loop"

        if not raw_response:
            logger.warning("LLM judge returned empty response")
            return False, "ERROR: LLM judge returned empty response"

        approved, reason = parse_judge(raw_response)
        logger.info("Judge verdict: %s — %s", approved, reason)
        return approved, reason

    except Exception as e:
        logger.error("LLM judge call failed: %s", e, exc_info=True)
        detail = str(e).strip() or type(e).__name__
        return False, f"ERROR: LLM judge call failed: {detail}"
