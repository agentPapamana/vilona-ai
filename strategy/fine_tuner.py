"""Autonomous Real-Time Fine-Tuner Agent powered by 'antigravity-series'.

Calibrates live execution triggers (pullback band, thin TP, tight SL, min_confidence)
on-the-fly to guarantee maximum opportunity capture and rapid profit realization.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from strategy.config import (
    DEFAULT_CONFIG_PATH,
    ScalpConfigFile,
    load_scalp_config,
    save_scalp_config,
)

logger = logging.getLogger(__name__)

TUNER_SYSTEM_PROMPT = """You are 'FineTuner', an aggressive real-time execution tuner AI for the KINGAI 5M Scalping Strategy.
Your role: Optimize the active live scalping configuration (scalp.yaml) on-the-fly to ensure the bot strikes immediately when momentum shifts, capturing fast profits without missing opportunities.

DIAGNOSTIC & TUNING RULES:
1. RAPID ENTRY EXECUTION:
   - If distance from price to EMA9 is wide (e.g. Gold moving rapidly), widen 'atr_multiplier_band' to 2.0 - 2.5 so entries are not held back.
   - Set 'min_confidence' aggressively between 0.50 and 0.54. Never let it sit too high (e.g. >0.60) which causes prolonged WAIT status.
2. ULTRA-FAST TAKE PROFIT:
   - Set 'tp_points' between 1.0 and 1.5 points (10-15 pipettes on Gold). Fast in, fast out.
3. STRICT STOP LOSS & MICRO-BE:
   - 'sl_points': 5.0 to 6.5 points (always respects broker min stop).
   - 'micro_be_points': 0.6 to 0.9 points (lock modal instantly once price moves +0.8 pt).
4. SPREAD TOLERANCE:
   - 'max_spread_points': 1.2 to 1.5 points (allow normal Gold cent spreads).

Return valid JSON ONLY in this exact format:
{
  "tp_points": 1.2,
  "sl_points": 6.0,
  "micro_be_points": 0.8,
  "max_spread_points": 1.4,
  "min_confidence": 0.52,
  "atr_multiplier_band": 2.2,
  "reason": "short explanation of tuning"
}"""


class ScalpFineTuner:
    """Real-time fine-tuner agent powered by antigravity-series."""

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG_PATH,
        model_name: str = "antigravity-series",
    ) -> None:
        self.config_path = config_path
        self.model_name = model_name
        self._last_tune_time: float = 0.0

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        if not text or not text.strip():
            return None
        t = text.strip()
        if "```" in t:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t)
            if match:
                t = match.group(1).strip()
        try:
            parsed = json.loads(t)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    def tune_symbol(
        self,
        symbol: str,
        current_tick: Any,
        candles_5m: list,
        atr_5m: float,
        dist_to_ema: float,
        client: Optional[Any] = None,
        timeout_s: float = 15.0,
    ) -> bool:
        """Call antigravity-series model to adjust scalp.yaml parameters dynamically."""
        if client is None:
            return False

        config_file = load_scalp_config(self.config_path)
        sym_cfg = config_file.get_symbol_config(symbol)

        payload = {
            "symbol": symbol,
            "bid": getattr(current_tick, "bid", 0.0),
            "ask": getattr(current_tick, "ask", 0.0),
            "spread": max(0.0, float(getattr(current_tick, "ask", 0.0)) - float(getattr(current_tick, "bid", 0.0))),
            "atr_5m": atr_5m,
            "dist_to_ema9": dist_to_ema,
            "current_band": sym_cfg.indicators.atr_multiplier_band,
            "current_tp": sym_cfg.tp_points,
            "current_sl": sym_cfg.sl_points,
            "current_min_confidence": sym_cfg.min_confidence,
        }

        user_prompt = f"Optimize live parameters for instant entry and fast TP:\n{json.dumps(payload, indent=2)}"

        try:
            generator = getattr(client, "generate_signal", None)
            if not callable(generator):
                return False

            result = generator(TUNER_SYSTEM_PROMPT, user_prompt, model=self.model_name)
            if hasattr(result, "__await__"):
                async def run():
                    try:
                        return await asyncio.wait_for(result, timeout=timeout_s)
                    finally:
                        closer = getattr(client, "close", None)
                        if callable(closer):
                            try:
                                await asyncio.wait_for(closer(), timeout=2)
                            except Exception:
                                pass
                raw_response = asyncio.run(run())
            else:
                raw_response = str(result) if result else None

            if not raw_response:
                return False

            tuning = self._extract_json(raw_response)
            if not tuning:
                return False

            # Apply tuning safely bounded by invariants
            sym_cfg.tp_points = max(1.0, min(2.0, float(tuning.get("tp_points", sym_cfg.tp_points))))
            sym_cfg.sl_points = max(5.0, min(8.0, float(tuning.get("sl_points", sym_cfg.sl_points))))
            sym_cfg.micro_be_points = max(0.6, min(1.0, float(tuning.get("micro_be_points", sym_cfg.micro_be_points))))
            sym_cfg.max_spread_points = max(1.0, min(1.6, float(tuning.get("max_spread_points", sym_cfg.max_spread_points))))
            sym_cfg.min_confidence = max(0.48, min(0.56, float(tuning.get("min_confidence", sym_cfg.min_confidence))))
            sym_cfg.indicators.atr_multiplier_band = max(1.8, min(2.6, float(tuning.get("atr_multiplier_band", sym_cfg.indicators.atr_multiplier_band))))
            sym_cfg.validate()

            config_file.symbols[symbol] = sym_cfg
            config_file.ai_orchestrator["fine_tuner_model"] = self.model_name
            config_file.ai_orchestrator["last_fine_tune"] = datetime.now(timezone.utc).isoformat()
            config_file.ai_orchestrator["fine_tune_reason"] = tuning.get("reason", "finetuner aggressive optimization")

            save_scalp_config(config_file, self.config_path)
            logger.info(
                "[FINETUNER] '%s' applied: TP=%.2f, SL=%.2f, BE=%.2f, Band=%.2f, MinConf=%.2f (%s)",
                self.model_name,
                sym_cfg.tp_points,
                sym_cfg.sl_points,
                sym_cfg.micro_be_points,
                sym_cfg.indicators.atr_multiplier_band,
                sym_cfg.min_confidence,
                tuning.get("reason", "tuned"),
            )
            return True
        except Exception as exc:
            logger.warning("[FINETUNER] Failed tuning with %s: %s", self.model_name, exc)
            return False
