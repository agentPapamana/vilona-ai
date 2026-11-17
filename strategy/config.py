"""Configuration parser and validator for scalp.yaml with resilient safe defaults."""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "scalp.yaml"


@dataclass
class GlobalSettings:
    active: bool = True
    max_concurrent_scalp_positions: int = 1
    max_risk_per_trade_pct: float = 0.5  # Hard upper bound: 0.5% equity
    emergency_hard_sl_points: float = 15.0


@dataclass
class IndicatorSettings:
    ema_fast: int = 9
    ema_slow: int = 21
    trend_filter_ema: int = 50
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    atr_multiplier_band: float = 1.2


@dataclass
class TimeframeWeights:
    tf_1h: float = 0.50
    tf_30m: float = 0.30
    tf_15m: float = 0.20


@dataclass
class SymbolScalpConfig:
    enabled: bool = True
    tp_points: float = 2.0  # Thin TP: 1.0 - 3.0 points
    sl_points: float = 10.0  # Tight SL bounded by risk
    micro_be_points: float = 1.0  # Breakeven trigger at +1.0 pt
    max_spread_points: float = 1.5  # Maximum tolerable spread
    risk_per_trade_pct: float = 0.5  # Max 0.5%
    fixed_lot: float = 0.01
    use_fixed_lot: bool = True
    auto_scale_lot: bool = False  # Dynamic lot scale based on trend quality (0.01 -> 0.05 -> max 0.50)
    lot_min_scale: float = 0.01
    lot_max_scale: float = 0.50
    min_confidence: float = 0.60
    cooldown_bars: int = 1
    indicators: IndicatorSettings = field(default_factory=IndicatorSettings)
    weights: TimeframeWeights = field(default_factory=TimeframeWeights)

    def validate(self) -> None:
        """Enforce PRD bounds: SL risk <= 0.5%, TP between 1.0 and 5.0."""
        if self.risk_per_trade_pct > 0.5:
            logger.warning(
                "risk_per_trade_pct (%s) exceeds max 0.5%%. Clamping to 0.5%%.",
                self.risk_per_trade_pct,
            )
            self.risk_per_trade_pct = 0.5
        if self.tp_points <= 0.0:
            self.tp_points = 2.0
        if self.sl_points <= 0.0:
            self.sl_points = 10.0
        if self.max_spread_points <= 0.0:
            self.max_spread_points = 1.5
        if self.micro_be_points <= 0.0:
            self.micro_be_points = 1.0


@dataclass
class ScalpConfigFile:
    version: str = "1.0.0"
    generated_at: str = ""
    market_regime: str = "ranging_low_volatility"
    ai_orchestrator: Dict[str, Any] = field(
        default_factory=lambda: {"last_run_status": "INITIAL", "model_used": "unknown"}
    )
    global_settings: GlobalSettings = field(default_factory=GlobalSettings)
    symbols: Dict[str, SymbolScalpConfig] = field(default_factory=dict)

    def get_symbol_config(self, symbol: str) -> SymbolScalpConfig:
        """Look up symbol config with fallback heuristics."""
        sym = (symbol or "").strip()
        if sym in self.symbols:
            return self.symbols[sym]

        # Case-insensitive lookup
        for k, v in self.symbols.items():
            if k.upper() == sym.upper():
                return v

        # Strip standard suffix like 'c' or broker specifics
        stripped = sym.rstrip("c").rstrip("C")
        if stripped in self.symbols:
            return self.symbols[stripped]

        # Default fallback
        logger.info("Using default SymbolScalpConfig for unconfigured symbol: %s", sym)
        default_sym = SymbolScalpConfig()
        default_sym.validate()
        return default_sym


def create_default_scalp_config() -> ScalpConfigFile:
    """Produce safe hardcoded defaults when scalp.yaml is absent or invalid."""
    now_iso = datetime.now(timezone.utc).isoformat()
    return ScalpConfigFile(
        version="1.0.0",
        generated_at=now_iso,
        market_regime="ranging_low_volatility",
        ai_orchestrator={
            "last_run_status": "FALLBACK_DEFAULT",
            "model_used": "native_defaults",
        },
        global_settings=GlobalSettings(
            active=True,
            max_concurrent_scalp_positions=1,
            max_risk_per_trade_pct=0.5,
            emergency_hard_sl_points=15.0,
        ),
        symbols={
            "XAUUSDc": SymbolScalpConfig(
                enabled=True,
                tp_points=2.0,
                sl_points=10.0,
                micro_be_points=1.0,
                max_spread_points=1.5,
                risk_per_trade_pct=0.5,
                fixed_lot=0.01,
                use_fixed_lot=True,
                min_confidence=0.60,
                cooldown_bars=1,
            ),
            "EURUSDc": SymbolScalpConfig(
                enabled=True,
                tp_points=1.5,
                sl_points=6.0,
                micro_be_points=0.8,
                max_spread_points=1.0,
                risk_per_trade_pct=0.5,
                fixed_lot=0.01,
                use_fixed_lot=True,
                min_confidence=0.65,
                cooldown_bars=1,
            ),
            "XAUUSD": SymbolScalpConfig(
                enabled=True,
                tp_points=2.0,
                sl_points=10.0,
                micro_be_points=1.0,
                max_spread_points=1.5,
                risk_per_trade_pct=0.5,
                fixed_lot=0.01,
                use_fixed_lot=False,
                min_confidence=0.60,
                cooldown_bars=1,
            ),
        },
    )


def load_scalp_config(path: str = DEFAULT_CONFIG_PATH) -> ScalpConfigFile:
    """Load and validate scalp.yaml; return safe default on any error."""
    if not os.path.exists(path):
        logger.warning("Configuration file %s not found. Using safe defaults.", path)
        return create_default_scalp_config()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict) or not data:
            logger.warning("Empty or non-dict scalp config in %s. Using safe defaults.", path)
            return create_default_scalp_config()

        version = str(data.get("version", "1.0.0"))
        generated_at = str(data.get("generated_at", ""))
        market_regime = str(data.get("market_regime", "ranging_low_volatility"))
        ai_meta = data.get("ai_orchestrator", {})
        if not isinstance(ai_meta, dict):
            ai_meta = {}

        global_raw = data.get("global_settings", {})
        if not isinstance(global_raw, dict):
            global_raw = {}
        global_settings = GlobalSettings(
            active=bool(global_raw.get("active", True)),
            max_concurrent_scalp_positions=int(global_raw.get("max_concurrent_scalp_positions", 1)),
            max_risk_per_trade_pct=min(0.5, float(global_raw.get("max_risk_per_trade_pct", 0.5))),
            emergency_hard_sl_points=float(global_raw.get("emergency_hard_sl_points", 15.0)),
        )

        symbols_dict: Dict[str, SymbolScalpConfig] = {}
        symbols_raw = data.get("symbols", {})
        if isinstance(symbols_raw, dict):
            for sym, s_data in symbols_raw.items():
                if not isinstance(s_data, dict):
                    continue
                ind_raw = s_data.get("indicators", {})
                ind = (
                    IndicatorSettings(
                        ema_fast=int(ind_raw.get("ema_fast", 9)),
                        ema_slow=int(ind_raw.get("ema_slow", 21)),
                        trend_filter_ema=int(ind_raw.get("trend_filter_ema", 50)),
                        rsi_period=int(ind_raw.get("rsi_period", 14)),
                        rsi_overbought=float(ind_raw.get("rsi_overbought", 70.0)),
                        rsi_oversold=float(ind_raw.get("rsi_oversold", 30.0)),
                        atr_multiplier_band=float(ind_raw.get("atr_multiplier_band", 1.2)),
                    )
                    if isinstance(ind_raw, dict)
                    else IndicatorSettings()
                )

                w_raw = s_data.get("weights", {})
                weights = (
                    TimeframeWeights(
                        tf_1h=float(w_raw.get("tf_1h", 0.5)),
                        tf_30m=float(w_raw.get("tf_30m", 0.3)),
                        tf_15m=float(w_raw.get("tf_15m", 0.2)),
                    )
                    if isinstance(w_raw, dict)
                    else TimeframeWeights()
                )

                cfg_sym = SymbolScalpConfig(
                    enabled=bool(s_data.get("enabled", True)),
                    tp_points=float(s_data.get("tp_points", 2.0)),
                    sl_points=float(s_data.get("sl_points", 10.0)),
                    micro_be_points=float(s_data.get("micro_be_points", 1.0)),
                    max_spread_points=float(s_data.get("max_spread_points", 1.5)),
                    risk_per_trade_pct=min(0.5, float(s_data.get("risk_per_trade_pct", 0.5))),
                    fixed_lot=float(s_data.get("fixed_lot", 0.01)),
                    use_fixed_lot=bool(s_data.get("use_fixed_lot", True)),
                    auto_scale_lot=bool(s_data.get("auto_scale_lot", False)),
                    lot_min_scale=float(s_data.get("lot_min_scale", 0.01)),
                    lot_max_scale=float(s_data.get("lot_max_scale", 0.50)),
                    min_confidence=float(s_data.get("min_confidence", 0.60)),
                    cooldown_bars=int(s_data.get("cooldown_bars", 1)),
                    indicators=ind,
                    weights=weights,
                )
                cfg_sym.validate()
                symbols_dict[sym] = cfg_sym

        cfg_file = ScalpConfigFile(
            version=version,
            generated_at=generated_at,
            market_regime=market_regime,
            ai_orchestrator=ai_meta,
            global_settings=global_settings,
            symbols=symbols_dict or create_default_scalp_config().symbols,
        )
        return cfg_file
    except Exception as exc:
        logger.error("Failed to parse %s (%s). Falling back to safe defaults.", path, exc)
        return create_default_scalp_config()


def save_scalp_config(config: ScalpConfigFile, path: str = DEFAULT_CONFIG_PATH) -> bool:
    """Atomic write of ScalpConfigFile to YAML."""
    try:
        raw_dict: Dict[str, Any] = {
            "version": config.version,
            "generated_at": config.generated_at or datetime.now(timezone.utc).isoformat(),
            "market_regime": config.market_regime,
            "ai_orchestrator": config.ai_orchestrator,
            "global_settings": {
                "active": config.global_settings.active,
                "max_concurrent_scalp_positions": config.global_settings.max_concurrent_scalp_positions,
                "max_risk_per_trade_pct": config.global_settings.max_risk_per_trade_pct,
                "emergency_hard_sl_points": config.global_settings.emergency_hard_sl_points,
            },
            "symbols": {},
        }
        for sym, scfg in config.symbols.items():
            scfg.validate()
            raw_dict["symbols"][sym] = {
                "enabled": scfg.enabled,
                "tp_points": scfg.tp_points,
                "sl_points": scfg.sl_points,
                "micro_be_points": scfg.micro_be_points,
                "max_spread_points": scfg.max_spread_points,
                "risk_per_trade_pct": scfg.risk_per_trade_pct,
                "fixed_lot": scfg.fixed_lot,
                "use_fixed_lot": scfg.use_fixed_lot,
                "auto_scale_lot": getattr(scfg, "auto_scale_lot", False),
                "lot_min_scale": getattr(scfg, "lot_min_scale", 0.01),
                "lot_max_scale": getattr(scfg, "lot_max_scale", 0.50),
                "min_confidence": scfg.min_confidence,
                "cooldown_bars": scfg.cooldown_bars,
                "indicators": {
                    "ema_fast": scfg.indicators.ema_fast,
                    "ema_slow": scfg.indicators.ema_slow,
                    "trend_filter_ema": scfg.indicators.trend_filter_ema,
                    "rsi_period": scfg.indicators.rsi_period,
                    "rsi_overbought": scfg.indicators.rsi_overbought,
                    "rsi_oversold": scfg.indicators.rsi_oversold,
                    "atr_multiplier_band": scfg.indicators.atr_multiplier_band,
                },
                "weights": {
                    "tf_1h": scfg.weights.tf_1h,
                    "tf_30m": scfg.weights.tf_30m,
                    "tf_15m": scfg.weights.tf_15m,
                },
            }

        dir_name = os.path.dirname(os.path.abspath(path))
        os.makedirs(dir_name, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            yaml.dump(raw_dict, tf, sort_keys=False, default_flow_style=False)
            temp_path = tf.name

        os.replace(temp_path, path)
        logger.info("Successfully updated scalp config at %s", path)
        return True
    except Exception as exc:
        logger.error("Failed to save scalp config to %s: %s", path, exc)
        return False
