import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cost_volatility_test as cost_test
from config import Config
from backtest_strategy import DEFAULT_COMMISSION_PER_LOT, DEFAULT_SPREAD_PRICE, run_symbol_scalp


def _bars():
    path = Path(__file__).parent / "fixtures" / "cost_volatility_xauusd_m5.json"
    return json.loads(path.read_text())


def test_cost_screen_reproduces_reference_scalp_backtest():
    bars = _bars()
    cfg = Config.from_env()
    cfg.trading.sl_atr_mult = 2.5
    cfg.trading.tp_atr_mult = 3.0
    cfg.trading.min_confidence = 0.55
    cfg.trading.max_risk_pct = 0.5
    cfg.trading.min_lot = 0.01
    cfg.trading.max_lot = 0.01
    cfg.trading.lot_step = 0.01
    cost = cost_test.simulate("XAUUSD", bars, cfg, DEFAULT_SPREAD_PRICE["XAUUSD"],
                              DEFAULT_COMMISSION_PER_LOT, Path("data/candles"))
    reference = run_symbol_scalp("XAUUSD", bars, cfg, DEFAULT_SPREAD_PRICE["XAUUSD"],
                                 DEFAULT_COMMISSION_PER_LOT, 0.5, 10_000.0,
                                 True, Path("data/candles"))
    assert len(cost.trades) == len(reference.trades)
    assert [t.pnl_usd for t in cost.trades] == [t.pnl_usd for t in reference.trades]
    assert [t.entry_time for t in cost.trades] == [t.entry_time for t in reference.trades]
