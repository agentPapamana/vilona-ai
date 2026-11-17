"""Resilient LLM Manager ('Scalper') Orchestrator.

Generates and tunes dynamic scalp.yaml parameters from multi-timeframe backtest
and execution telemetry. 100% fault-tolerant: if AI is disconnected, the strategy
falls back safely to native defaults without interrupting trading.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from strategy.config import (
    DEFAULT_CONFIG_PATH,
    GlobalSettings,
    IndicatorSettings,
    ScalpConfigFile,
    SymbolScalpConfig,
    TimeframeWeights,
    create_default_scalp_config,
    load_scalp_config,
    save_scalp_config,
)

logger = logging.getLogger(__name__)

SCALPER_SYSTEM_PROMPT = """You are 'Scalper', the aggressive high-frequency AI Orchestrator for the KINGAI 5M Scalping Strategy.
Your primary mission is HIGH-OPPORTUNITY EXECUTION: ensure the bot actively captures valid 5-minute setups rapidly, avoiding prolonged inactivity, while maintaining strict mathematical edge and capital defense.

HIGH-FREQUENCY TUNING PRINCIPLES (MANDATORY):
1. High Signal Agility ('min_confidence'): Calibrate 'min_confidence' between 0.50 and 0.54 so valid momentum and pullback setups are immediately triggered every 5 minutes rather than skipped.
2. Volatility Tolerance ('atr_multiplier_band'): Fast instruments like Gold (XAUUSD) move in wide waves. Set 'atr_multiplier_band' between 1.8 and 2.5 so pullbacks are captured even when price pulls away from EMA9.
3. Ultra-Fast Profit Realization ('tp_points'): Set thin take profits between 1.0 and 1.8 points (10-18 pipettes). Fast in, fast out, locking gains instantly.
4. Dynamic Capital Defense ('sl_points'): Set 'sl_points' between 5.0 and 8.0 points. Never exceed 0.5% account equity risk.
5. Micro-Breakeven Armor ('micro_be_points'): Set between 0.8 and 1.0 points. Once the position gains +1 point, stop moves to entry+0.1 to eliminate loss.
6. Spread Gate ('max_spread_points'): Set between 1.2 and 1.6 points to tolerate standard Gold cent broker spreads without unnecessary blocking.

Respond with valid JSON only in the following schema:
{
  "market_regime": "ranging_low_volatility|trending_up|trending_down|high_volatility",
  "global_settings": {
    "active": true,
    "max_concurrent_scalp_positions": 1,
    "max_risk_per_trade_pct": 0.5,
    "emergency_hard_sl_points": 15.0
  },
  "symbols": {
    "<SYMBOL>": {
      "enabled": true,
      "tp_points": 2.0,
      "sl_points": 10.0,
      "micro_be_points": 1.0,
      "max_spread_points": 1.5,
      "risk_per_trade_pct": 0.5,
      "fixed_lot": 0.01,
      "use_fixed_lot": true,
      "min_confidence": 0.60,
      "cooldown_bars": 1,
      "indicators": {
        "ema_fast": 9,
        "ema_slow": 21,
        "trend_filter_ema": 50,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "atr_multiplier_band": 1.2
      },
      "weights": {
        "tf_1h": 0.50,
        "tf_30m": 0.30,
        "tf_15m": 0.20
      }
    }
  }
}"""


class ScalperLLMOrchestrator:
    """Manages AI-driven parameter calibration with offline resilience."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self.config_path = config_path

    @staticmethod
    def _call_llm_safe(client: Any, system_prompt: str, user_prompt: str, timeout_s: float) -> Optional[str]:
        """Call LLM client with timeout and complete exception safety."""
        try:
            generator = getattr(client, "generate_signal", None)
            if not callable(generator):
                return None
            result = generator(system_prompt, user_prompt)
            if hasattr(result, "__await__"):
                async def run():
                    try:
                        return await asyncio.wait_for(result, timeout=timeout_s)
                    finally:
                        closer = getattr(client, "close", None)
                        if callable(closer):
                            try:
                                await asyncio.wait_for(closer(), timeout=3)
                            except Exception:
                                pass
                return asyncio.run(run())
            return str(result) if result else None
        except Exception as exc:
            logger.warning("[SCALPER] AI request failed or timed out: %s", exc)
            return None

    @staticmethod
    def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
        """Extract JSON object from markdown codeblock or raw string."""
        if not raw or not raw.strip():
            return None
        text = raw.strip()
        # Strip markdown fences if present
        if "```" in text:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
            if match:
                text = match.group(1).strip()
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _apply_backtest_to_config(
        config: ScalpConfigFile,
        backtest_data: Dict[str, Any],
        status: str,
        model_used: str = "native_backtester",
    ) -> None:
        """Update config parameters directly from backtest recommendations."""
        config.generated_at = datetime.now(timezone.utc).isoformat()
        config.ai_orchestrator = {
            "last_run_status": status,
            "last_attempt": datetime.now(timezone.utc).isoformat(),
            "model_used": model_used,
        }
        if not isinstance(backtest_data, dict):
            return

        for sym, bdata in backtest_data.items():
            if not isinstance(bdata, dict):
                continue
            regime = bdata.get("volatility_regime")
            if regime:
                config.market_regime = regime
            rec_tp = bdata.get("recommended_tp_points")
            rec_sl = bdata.get("recommended_sl_points")

            sym_cfg = config.get_symbol_config(sym)
            if rec_tp is not None:
                sym_cfg.tp_points = max(1.0, min(3.0, round(float(rec_tp), 1)))
            if rec_sl is not None:
                sym_cfg.sl_points = max(5.0, min(15.0, round(float(rec_sl), 1)))
            sym_cfg.validate()
            config.symbols[sym] = sym_cfg
    def formulate_config(
        self,
        backtest_data: Dict[str, Any],
        memory_context: Dict[str, Any],
        client: Optional[Any] = None,
        model_name: str = "scalper-llm",
        timeout_s: float = 12.0,
    ) -> ScalpConfigFile:
        """Formulate updated scalp configuration from backtest and telemetry.

        If AI is disconnected or fails, returns existing active config or safe defaults.
        """
        existing = load_scalp_config(self.config_path)

        if client is None:
            logger.info("[SCALPER] No LLM client supplied. Calibrating scalp.yaml with backtest metrics (native mode).")
            self._apply_backtest_to_config(existing, backtest_data, status="AI_OFFLINE_NATIVE_ACTIVE")
            save_scalp_config(existing, self.config_path)
            return existing
        payload = {
            "backtest_multi_timeframe": backtest_data,
            "telemetry_memory": memory_context,
            "current_config": {
                "market_regime": existing.market_regime,
                "symbols": {k: {"tp_points": v.tp_points, "sl_points": v.sl_points} for k, v in existing.symbols.items()},
            },
        }

        user_prompt = f"Calibrate M5 scalping parameters based on this data:\n{json.dumps(payload, indent=2)}"
        logger.info("[SCALPER] Requesting dynamic parameter optimization from LLM...")

        raw_response = self._call_llm_safe(
            client=client,
            system_prompt=SCALPER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            timeout_s=timeout_s,
        )

        if not raw_response:
            logger.warning("[SCALPER] AI disconnect detected. Calibrating scalp.yaml directly from backtest recommendations.")
            self._apply_backtest_to_config(existing, backtest_data, status="DISCONNECTED", model_used=model_name)
            save_scalp_config(existing, self.config_path)
            return existing
        parsed_json = self._extract_json(raw_response)
        if not parsed_json:
            logger.warning("[SCALPER] Invalid JSON received from AI. Continuing with active configuration.")
            existing.ai_orchestrator["last_run_status"] = "INVALID_RESPONSE_FALLBACK"
            return existing

        try:
            # Construct ScalpConfigFile from parsed JSON
            regime = str(parsed_json.get("market_regime", existing.market_regime))
            g_raw = parsed_json.get("global_settings", {})
            global_settings = GlobalSettings(
                active=bool(g_raw.get("active", True)),
                max_concurrent_scalp_positions=int(g_raw.get("max_concurrent_scalp_positions", 1)),
                max_risk_per_trade_pct=min(0.5, float(g_raw.get("max_risk_per_trade_pct", 0.5))),
                emergency_hard_sl_points=float(g_raw.get("emergency_hard_sl_points", 15.0)),
            )

            symbols_dict: Dict[str, SymbolScalpConfig] = {}
            for sym, s_data in parsed_json.get("symbols", {}).items():
                if not isinstance(s_data, dict):
                    continue
                ind_raw = s_data.get("indicators", {})
                w_raw = s_data.get("weights", {})
                sym_cfg = SymbolScalpConfig(
                    enabled=bool(s_data.get("enabled", True)),
                    tp_points=max(1.0, min(3.0, float(s_data.get("tp_points", 2.0)))),
                    sl_points=max(5.0, min(15.0, float(s_data.get("sl_points", 10.0)))),
                    micro_be_points=float(s_data.get("micro_be_points", 1.0)),
                    max_spread_points=float(s_data.get("max_spread_points", 1.5)),
                    risk_per_trade_pct=min(0.5, float(s_data.get("risk_per_trade_pct", 0.5))),
                    fixed_lot=float(s_data.get("fixed_lot", 0.01)),
                    use_fixed_lot=bool(s_data.get("use_fixed_lot", True)),
                    min_confidence=min(0.54, max(0.48, float(s_data.get("min_confidence", 0.52)))),
                    cooldown_bars=int(s_data.get("cooldown_bars", 1)),
                    indicators=IndicatorSettings(
                        ema_fast=int(ind_raw.get("ema_fast", 9)),
                        ema_slow=int(ind_raw.get("ema_slow", 21)),
                        trend_filter_ema=int(ind_raw.get("trend_filter_ema", 50)),
                        rsi_period=int(ind_raw.get("rsi_period", 14)),
                        rsi_overbought=float(ind_raw.get("rsi_overbought", 70.0)),
                        rsi_oversold=float(ind_raw.get("rsi_oversold", 30.0)),
                        atr_multiplier_band=max(2.0, min(3.0, float(ind_raw.get("atr_multiplier_band", 2.2)))),
                    ),
                    weights=TimeframeWeights(
                        tf_1h=float(w_raw.get("tf_1h", 0.50)),
                        tf_30m=float(w_raw.get("tf_30m", 0.30)),
                        tf_15m=float(w_raw.get("tf_15m", 0.20)),
                    ),
                )
                sym_cfg.validate()
                symbols_dict[sym] = sym_cfg

            new_config = ScalpConfigFile(
                version="1.0.0",
                generated_at=datetime.now(timezone.utc).isoformat(),
                market_regime=regime,
                ai_orchestrator={
                    "last_run_status": "SUCCESS",
                    "model_used": model_name,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                global_settings=global_settings,
                symbols=symbols_dict or existing.symbols,
            )
            save_scalp_config(new_config, self.config_path)
            logger.info("[SCALPER] Successfully generated and applied dynamic scalp.yaml via AI.")
            return new_config
        except Exception as exc:
            logger.error("[SCALPER] Failed processing AI response: %s. Using active config.", exc)
            return existing
