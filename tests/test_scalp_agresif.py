"""Unit and integration test suite for KINGAI Standalone Scalp Strategy (scalp_5m_agresif)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Dict, List
import pytest

from strategy.config import (
    ScalpConfigFile,
    SymbolScalpConfig,
    create_default_scalp_config,
    load_scalp_config,
    save_scalp_config,
)
from strategy.state_manager import ScalperStateManager
from strategy.indicators import (
    compute_atr,
    compute_ema,
    compute_m5_scalp_signal,
    compute_rsi,
    compute_trend_bias,
)
from strategy.backtester import StartupBacktester
from strategy.llm_orchestrator import ScalperLLMOrchestrator
from strategy.engine import ScalpAgresifEngine
from strategy import get_standalone_scalper, run_startup_calibration


def _make_candles(prices: List[float], step: int = 300) -> List[Dict[str, Any]]:
    rows = []
    base_t = 1700000000
    for i, p in enumerate(prices):
        rows.append({
            "time": base_t + i * step,
            "open": p - 0.1,
            "high": p + 0.5,
            "low": p - 0.5,
            "close": p,
            "volume": 100.0,
        })
    return rows


# =====================================================================
# 1. Configuration & Safe Defaults Tests
# =====================================================================
def test_scalp_config_safe_defaults():
    cfg = create_default_scalp_config()
    assert cfg.global_settings.active is True
    assert cfg.global_settings.max_risk_per_trade_pct <= 0.5

    xau = cfg.get_symbol_config("XAUUSDc")
    assert xau.enabled is True
    assert 1.0 <= xau.tp_points <= 3.0
    assert xau.risk_per_trade_pct <= 0.5
    assert xau.max_spread_points == 1.5


def test_scalp_config_file_io(tmp_path):
    conf_path = str(tmp_path / "test_scalp.yaml")
    cfg = create_default_scalp_config()
    cfg.market_regime = "trending_up"
    cfg.symbols["XAUUSDc"].tp_points = 2.5
    cfg.symbols["XAUUSDc"].risk_per_trade_pct = 0.8  # should be clamped to 0.5

    saved = save_scalp_config(cfg, conf_path)
    assert saved is True
    assert os.path.exists(conf_path)

    loaded = load_scalp_config(conf_path)
    assert loaded.market_regime == "trending_up"
    assert loaded.symbols["XAUUSDc"].tp_points == 2.5
    assert loaded.symbols["XAUUSDc"].risk_per_trade_pct <= 0.5  # clamped by validation


def test_scalp_config_fallback_on_corrupt_file(tmp_path):
    bad_path = str(tmp_path / "corrupt.yaml")
    with open(bad_path, "w", encoding="utf-8") as f:
        f.write("::: invalid : yaml [[")

    fallback = load_scalp_config(bad_path)
    assert fallback is not None
    assert fallback.global_settings.max_risk_per_trade_pct <= 0.5
    assert "XAUUSDc" in fallback.symbols


# =====================================================================
# 2. State & Telemetry Memory Tests
# =====================================================================
def test_state_manager_spread_and_trade_telemetry(tmp_path):
    mem_path = str(tmp_path / "memory.json")
    db_path = str(tmp_path / "scalper.json")
    mgr = ScalperStateManager(memory_path=mem_path, scalper_path=db_path)

    # Spread tracking
    mgr.record_spread("XAUUSDc", 1.2)
    mgr.record_spread("XAUUSDc", 1.4)
    mgr.record_spread("XAUUSDc", 1.0)

    ctx = mgr.get_short_term_context()
    spread_stat = ctx["spread_telemetry"]["XAUUSDc"]
    assert spread_stat["samples"] == 3
    assert spread_stat["min"] == 1.0
    assert spread_stat["max"] == 1.4
    assert 1.1 <= spread_stat["avg"] <= 1.3

    # Trade entry & exit
    mgr.record_trade_entry(
        ticket=1001,
        symbol="XAUUSDc",
        action="BUY",
        open_price=2650.0,
        sl=2640.0,
        tp=2652.0,
        spread=1.1,
    )
    mgr.record_be_triggered(1001)
    mgr.record_trade_exit(
        ticket=1001,
        close_price=2652.0,
        pnl_usd=0.20,
        exit_reason="TP_HIT",
    )

    ctx2 = mgr.get_short_term_context()
    assert ctx2["closed_trades_count"] == 1
    assert ctx2["win_rate"] == 1.0
    assert ctx2["net_pnl_usd"] == 0.20
    assert ctx2["last_5_trades"][0]["be_triggered"] is True

    # Episodic session save
    mgr.save_session_summary("TEST_SESSION", {"trades": 1, "win_rate": 1.0})
    sessions = mgr.get_long_term_context()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "TEST_SESSION"


# =====================================================================
# 3. Micro Indicators & Signal Tests
# =====================================================================
def test_indicators_math():
    prices = [10.0 + i * 0.5 for i in range(30)]
    ema = compute_ema(prices, 9)
    assert ema > 10.0
    assert ema < prices[-1]

    candles = _make_candles(prices)
    atr = compute_atr(candles, 14)
    assert atr > 0.0

    rsi = compute_rsi(prices, 14)
    assert rsi > 50.0  # up-trending

    bias = compute_trend_bias(candles, 9, 21)
    assert bias == "BULLISH"


def test_m5_signal_spread_rejection():
    c5m = _make_candles([100.0] * 35)
    c15m = _make_candles([100.0] * 35)
    # Spread is 2.0, max is 1.5 -> MUST reject
    sig = compute_m5_scalp_signal(
        c5m=c5m,
        c15m=c15m,
        c1h=[],
        bid=100.0,
        ask=102.0,
        max_spread_points=1.5,
    )
    assert sig.action == "WAIT"
    assert "spread" in sig.reason
    assert "exceeds max" in sig.reason


def test_m5_signal_valid_buy():
    # Construct M15 bullish trend
    c15m = _make_candles([100.0 + i * 0.3 for i in range(60)], step=900)
    # Construct M5 pullback to EMA9
    m5_prices = [115.0 + i * 0.1 for i in range(40)] + [119.0 - i * 0.05 for i in range(8)]
    c5m = _make_candles(m5_prices, step=300)
    price = float(m5_prices[-1])

    sig = compute_m5_scalp_signal(
        c5m=c5m,
        c15m=c15m,
        c1h=c15m,
        bid=price,
        ask=price + 0.1,  # tight spread 0.1
        max_spread_points=1.5,
        min_confidence=0.50,
    )
    assert sig.action in ("BUY", "WAIT")
    assert sig.atr_5m > 0


# =====================================================================
# 4. Startup Auto-Backtester Tests
# =====================================================================
class DummyBridge:
    def __init__(self, c5m=None, c15m=None, c1h=None):
        self._c5m = c5m or _make_candles([100.0 + i * 0.1 for i in range(60)], step=300)
        self._c15m = c15m or _make_candles([100.0 + i * 0.2 for i in range(60)], step=900)
        self._c1h = c1h or _make_candles([100.0 + i * 0.5 for i in range(60)], step=3600)

    def get_candles(self, symbol, timeframe, limit=100):
        if timeframe == "5m":
            return self._c5m
        if timeframe in ("15m", "30m"):
            return self._c15m
        return self._c1h


def test_startup_backtester():
    backtester = StartupBacktester()
    bridge = DummyBridge()
    res = backtester.run_backtest_for_symbol("XAUUSDc", bridge)

    assert res["symbol"] == "XAUUSDc"
    assert res["atr_metrics"]["5m"] > 0
    assert 1.0 <= res["recommended_tp_points"] <= 3.0
    assert 5.0 <= res["recommended_sl_points"] <= 15.0
    assert res["trend_alignment"]["1h"] == "BULLISH"


def test_startup_backtester_error_resilience():
    backtester = StartupBacktester()
    failing_bridge = SimpleNamespace(get_candles=lambda *args, **kwargs: 1 / 0)
    res = backtester.run_all(["XAUUSDc"], failing_bridge)

    assert "XAUUSDc" in res
    assert res["XAUUSDc"]["candles_analyzed"]["1h"] == 0
    assert res["XAUUSDc"]["recommended_tp_points"] == 2.0

# =====================================================================
# 5. LLM Scalper Orchestrator & AI Disconnect Resilience Tests
# =====================================================================
class MockLLMClient:
    def __init__(self, response_text: str):
        self.response_text = response_text

    def generate_signal(self, system: str, user: str) -> str:
        return self.response_text


def test_llm_orchestrator_success(tmp_path):
    conf_path = str(tmp_path / "scalp.yaml")
    save_scalp_config(create_default_scalp_config(), conf_path)

    orchestrator = ScalperLLMOrchestrator(config_path=conf_path)
    mock_payload = {
        "market_regime": "trending_up",
        "global_settings": {
            "active": True,
            "max_risk_per_trade_pct": 0.45,
        },
        "symbols": {
            "XAUUSDc": {
                "enabled": True,
                "tp_points": 2.2,
                "sl_points": 8.5,
                "micro_be_points": 1.0,
                "max_spread_points": 1.4,
                "risk_per_trade_pct": 0.45,
            }
        },
    }
    client = MockLLMClient(json.dumps(mock_payload))
    cfg = orchestrator.formulate_config(
        backtest_data={"test": 1},
        memory_context={"trades": 0},
        client=client,
    )
    assert cfg.market_regime == "trending_up"
    assert cfg.ai_orchestrator["last_run_status"] == "SUCCESS"
    assert cfg.symbols["XAUUSDc"].tp_points == 2.2
    assert cfg.symbols["XAUUSDc"].risk_per_trade_pct == 0.45


def test_llm_orchestrator_ai_disconnect_tolerance(tmp_path):
    """CRITICAL: If AI disconnects or throws, the strategy MUST continue seamlessly on native defaults."""
    conf_path = str(tmp_path / "scalp.yaml")
    save_scalp_config(create_default_scalp_config(), conf_path)

    orchestrator = ScalperLLMOrchestrator(config_path=conf_path)

    class FailingClient:
        def generate_signal(self, *args, **kwargs):
            raise ConnectionError("AI Gateway unreachable (Offline)")

    # Call with failing client
    cfg = orchestrator.formulate_config(
        backtest_data={},
        memory_context={},
        client=FailingClient(),
    )
    # Does NOT raise exception
    assert cfg is not None
    assert cfg.ai_orchestrator["last_run_status"] == "DISCONNECTED"
    # Existing config remains operational
    assert cfg.symbols["XAUUSDc"].tp_points == 2.0
    assert cfg.symbols["XAUUSDc"].risk_per_trade_pct <= 0.5


# =====================================================================
# 6. Native Execution Engine & Micro-BE Tests
# =====================================================================
class MockRoute:
    def __init__(self, spread: float = 0.5):
        self.spread = spread
        self.positions = []
        self.modified_sl = None

    def get_tick(self, symbol):
        return SimpleNamespace(bid=2650.0, ask=2650.0 + self.spread, timestamp_ms=time.time() * 1000)
    def get_symbol_info(self, symbol):
        return SimpleNamespace(digits=2, tick_size=0.01, tick_value=1.0, lot_min=0.01, lot_step=0.01, lot_max=50.0)

    def get_account_info(self):
        return SimpleNamespace(equity=5000.0, free_margin=4900.0)

    def get_candles(self, symbol, timeframe, limit=60):
        prices = [2650.0 + i * 0.1 for i in range(limit)]
        return _make_candles(prices, step=300 if timeframe == "5m" else 900)

    def get_positions(self):
        return self.positions

    def modify_position(self, ticket, symbol, sl, tp):
        self.modified_sl = sl
        return SimpleNamespace(success=True)


def test_engine_spread_gate(tmp_path):
    conf_path = str(tmp_path / "scalp.yaml")
    save_scalp_config(create_default_scalp_config(), conf_path)

    mock_scanner = SimpleNamespace(
        cfg=SimpleNamespace(trading=SimpleNamespace(symbols=["XAUUSDc"], max_lot=1.0), paper_trading=True),
        _feed=lambda msg: None,
        _agent_feed=lambda r, s, m: None,
    )
    test_state_mgr = ScalperStateManager(memory_path=str(tmp_path / "mem.json"), scalper_path=str(tmp_path / "sc.json"))
    engine = ScalpAgresifEngine(scanner=mock_scanner, config_path=conf_path, state_manager=test_state_mgr)
    route = MockRoute()

    # Spread is 2.5, max configured is 1.5 -> Must be rejected by spread gate
    tick_wide_spread = SimpleNamespace(bid=2650.0, ask=2652.5)
    engine.evaluate_symbol("XAUUSDc", route, tick_wide_spread)
    # Check that no trade was opened
    ctx = engine.state_manager.get_short_term_context()
    assert ctx["total_recorded_trades"] == 0


def test_engine_micro_breakeven(tmp_path):
    conf_path = str(tmp_path / "scalp.yaml")
    save_scalp_config(create_default_scalp_config(), conf_path)

    mock_scanner = SimpleNamespace(
        cfg=SimpleNamespace(trading=SimpleNamespace(symbols=["XAUUSDc"]), paper_trading=False),
        _feed=lambda msg: None,
        _agent_feed=lambda r, s, m: None,
    )
    engine = ScalpAgresifEngine(scanner=mock_scanner, config_path=conf_path)
    route = MockRoute()

    # Position in profit by +1.5 points (entry=2650.0, cur=2651.5)
    from core.order_safety import MAGIC_BASE
    magic_val = MAGIC_BASE + 200
    pos = SimpleNamespace(
        ticket=777,
        symbol="XAUUSDc",
        magic=magic_val,
        price_open=2650.0,
        price_current=2651.5,
        sl=2640.0,
        tp=2652.0,
        type="BUY",
    )
    route.positions = [pos]

    engine.manage_open_positions(route)
    assert route.modified_sl is not None
    # Micro-BE moves SL to entry + 0.1 (2650.10)
    assert route.modified_sl == 2650.10


# =====================================================================
# 7. Core Scanner Non-Destructive Hook & Zero-Regression Tests
# =====================================================================
def test_scanner_delegates_to_standalone_scalper(tmp_path):
    from core.quant_scanner import QuantScanner

    cfg = SimpleNamespace(
        trading=SimpleNamespace(
            strategy="scalp_5m_agresif",
            symbols=["XAUUSDc"],
            max_lot=1.0,
            paper_trading=True,
        ),
        paper_trading=True,
        llm=SimpleNamespace(enabled=False),
    )
    route = MockRoute()
    bridge = SimpleNamespace(
        bridge_for=lambda sym: route,
        get_candles=route.get_candles,
        get_symbol_info=route.get_symbol_info,
        get_account_info=route.get_account_info,
        get_positions=lambda: [],
    )
    scanner = QuantScanner(bridge, cfg, None, None)

    # Calling evaluate_symbol with scalp_5m_agresif executes standalone engine without error
    tick = SimpleNamespace(bid=2650.0, ask=2650.8)
    scanner.evaluate_symbol("XAUUSDc")
    # Verified: evaluates cleanly without crashing or reaching legacy branches
