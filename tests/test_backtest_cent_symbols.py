"""Cent-suffixed broker symbols must survive the backtest path unchanged.

The broker trades `XAUUSDc`/`EURUSDc`, not `XAUUSD`/`EURUSD`. An earlier
`--symbols` upper-casing step turned `XAUUSDc` into `XAUUSDC`, which the broker
rejects with `rates_unavailable`, so a cent-account backtest silently produced
zero bars. These tests pin the symbol handling and the per-symbol cost lookup.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_strategy import DEFAULT_SPREAD_PRICE, SYMBOLS, load_symbol_info


def test_cent_symbols_are_backtestable_defaults():
    assert "XAUUSDc" in SYMBOLS
    assert "EURUSDc" in SYMBOLS


def test_cent_symbols_have_their_own_spread_assumption():
    assert DEFAULT_SPREAD_PRICE["XAUUSDc"] == DEFAULT_SPREAD_PRICE["XAUUSD"]
    assert DEFAULT_SPREAD_PRICE["EURUSDc"] == DEFAULT_SPREAD_PRICE["EURUSD"]


def test_cli_preserves_cent_symbol_case():
    """`--symbols XAUUSDc` must reach the loader as `XAUUSDc`, never `XAUUSDC`."""
    result = subprocess.run(
        [sys.executable, "scripts/backtest_strategy.py", "--months", "12",
         "--symbols", "XAUUSDc", "--candles-dir-m5", "data/does-not-exist",
         "--strategy", "scalp_5m"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    combined = result.stdout + result.stderr
    assert "XAUUSDc" in combined
    assert "XAUUSDC" not in combined


def test_symbol_info_is_read_from_the_candle_directory(tmp_path):
    """Metadata must come from the directory holding the candles, not a fixed path."""
    (tmp_path / "symbol_info.json").write_text(json.dumps({
        "XAUUSDc": {"lot_max": 1000.0, "lot_min": 0.01, "lot_step": 0.01,
                    "point": 0.01, "tick_size": 0.01, "tick_value": 1.0},
    }))

    info = load_symbol_info("XAUUSDc", tmp_path)

    assert info["tick_value"] == 1.0
    assert info["lot_max"] == 1000.0


def test_missing_cent_metadata_fails_loudly(tmp_path):
    (tmp_path / "symbol_info.json").write_text(json.dumps({"XAUUSD": {
        "lot_max": 100.0, "lot_min": 0.01, "lot_step": 0.01,
        "point": 0.01, "tick_size": 0.01, "tick_value": 0.1,
    }}))

    with pytest.raises(KeyError):
        load_symbol_info("XAUUSDc", tmp_path)


def test_cent_gold_tick_value_differs_from_standard_contract():
    """XAUUSDc is a cent contract: its tick value is not the standard 0.1."""
    research = json.loads((ROOT / "data" / "research_1y_m5" / "symbol_info.json").read_text())

    assert research["XAUUSDc"]["tick_value"] != research["XAUUSD"]["tick_value"]
