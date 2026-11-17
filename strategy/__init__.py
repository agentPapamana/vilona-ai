"""Standalone Scalp Strategy Package (`scalp_5m_agresif`).

Fully decoupled, zero-disruption integration with Vilona AI bot.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from strategy.backtester import StartupBacktester
from strategy.config import ScalpConfigFile, load_scalp_config, save_scalp_config
from strategy.engine import ScalpAgresifEngine
from strategy.llm_orchestrator import ScalperLLMOrchestrator
from strategy.fine_tuner import ScalpFineTuner
from strategy.state_manager import ScalperStateManager
logger = logging.getLogger(__name__)

_engine_instance: Optional[ScalpAgresifEngine] = None
_state_manager_instance: Optional[ScalperStateManager] = None


def get_state_manager() -> ScalperStateManager:
    """Retrieve shared telemetry state manager."""
    global _state_manager_instance
    if _state_manager_instance is None:
        _state_manager_instance = ScalperStateManager()
    return _state_manager_instance


def get_standalone_scalper(scanner: Any, config_path: str = "scalp.yaml") -> ScalpAgresifEngine:
    """Factory method for ScalpAgresifEngine singleton."""
    global _engine_instance
    if _engine_instance is None or _engine_instance.scanner is not scanner:
        state_mgr = get_state_manager()
        _engine_instance = ScalpAgresifEngine(
            scanner=scanner,
            config_path=config_path,
            state_manager=state_mgr,
        )
    return _engine_instance


def run_startup_calibration(scanner: Any, symbols: List[str]) -> Dict[str, Any]:
    """Execute automated startup backtest (1H, 30m, 15m) and AI parameter tuning.

    Guaranteed not to throw or block: if bridge/AI is unavailable, logs warning
    and retains active/safe configuration.
    """
    def _notify(msg: str):
        logger.info(msg)
        if scanner and hasattr(scanner, "_feed"):
            try:
                scanner._feed(msg)
            except Exception:
                pass
        if scanner and hasattr(scanner, "_agent_feed"):
            try:
                scanner._agent_feed("scalper", "CALIBRATE", msg)
            except Exception:
                pass

    _notify("[SCALPER BACKTEST] Initializing automated multi-timeframe backtest (1H, 30m, 15m, 5m)...")
    state_mgr = get_state_manager()
    backtester = StartupBacktester()

    bridge = getattr(scanner, "bridge", None)
    backtest_results: Dict[str, Any] = {}
    if bridge:
        for sym in symbols:
            _notify(f"[SCALPER BACKTEST] Running multi-TF simulation on {sym}...")
            try:
                res = backtester.run_backtest_for_symbol(sym, bridge)
                backtest_results[sym] = res
                wr = res.get("simulated_win_rate", 0.0)
                rec_tp = res.get("recommended_tp_points", 2.0)
                rec_sl = res.get("recommended_sl_points", 10.0)
                regime = res.get("volatility_regime", "moderate")
                _notify(
                    f"[SCALPER BACKTEST] {sym} Complete: WinRate={wr*100:.1f}%, "
                    f"Regime={regime}, Rec TP={rec_tp}pts, Rec SL={rec_sl}pts"
                )
            except Exception as exc:
                logger.error("[SCALP STARTUP] Error during backtest for %s: %s", sym, exc)
                _notify(f"[SCALPER BACKTEST] {sym} Failed: {exc}")
    llm_cfg = getattr(scanner.cfg, "llm", None) if scanner and hasattr(scanner, "cfg") else None
    ai_enabled = bool(getattr(llm_cfg, "enabled", False))

    orchestrator = ScalperLLMOrchestrator()
    memory_ctx = state_mgr.get_short_term_context()

    client = None
    if ai_enabled:
        try:
            from llm.client import get_llm_client

            client = get_llm_client()
        except Exception as exc:
            logger.warning("[SCALP STARTUP] Could not initialize LLM client: %s", exc)

    model_name = getattr(llm_cfg, "model", "scalper-llm")
    cfg = orchestrator.formulate_config(
        backtest_data=backtest_results,
        memory_context=memory_ctx,
        client=client,
        model_name=model_name,
        timeout_s=float(getattr(llm_cfg, "timeout", 30) or 30),
    )
    _notify(
        f"[SCALPER AI] Configuration updated into scalp.yaml | "
        f"Status: {cfg.ai_orchestrator.get('last_run_status', 'UNKNOWN')} | Regime: {cfg.market_regime}"
    )
    return {
        "status": cfg.ai_orchestrator.get("last_run_status", "UNKNOWN"),
        "market_regime": cfg.market_regime,
        "backtest_results": backtest_results,
    }


__all__ = [
    "ScalpAgresifEngine",
    "StartupBacktester",
    "ScalperLLMOrchestrator",
    "ScalpFineTuner",
    "ScalperStateManager",
    "load_scalp_config",
    "save_scalp_config",
    "get_standalone_scalper",
    "get_state_manager",
    "run_startup_calibration",
]
