"""The PAPER flag must have exactly one source of truth.

Previously `from_env` wrote `KINGAI_PAPER_TRADING` into
`config.trading.paper_trading`, while the scanner, dashboard and validator read
`config.paper_trading`. The env var could therefore never turn live mode on:
the scanner always believed paper mode was set.
"""

from __future__ import annotations

import importlib

import pytest


def _fresh_config(monkeypatch, paper_value: str):
    """Rebuild the config module so from_env() re-reads the environment."""
    import config as config_mod

    monkeypatch.setenv("KINGAI_PAPER_TRADING", paper_value)
    return config_mod.Config.from_env()


def test_env_false_disables_paper_on_the_attribute_the_scanner_reads(monkeypatch):
    cfg = _fresh_config(monkeypatch, "false")
    # quant_scanner.py: getattr(self.cfg, "paper_trading", False)
    assert cfg.paper_trading is False


def test_env_true_keeps_paper_on_the_attribute_the_scanner_reads(monkeypatch):
    cfg = _fresh_config(monkeypatch, "true")
    assert cfg.paper_trading is True


def test_trading_and_root_paper_flags_never_disagree(monkeypatch):
    cfg = _fresh_config(monkeypatch, "false")
    assert cfg.trading.paper_trading == cfg.paper_trading

    cfg = _fresh_config(monkeypatch, "true")
    assert cfg.trading.paper_trading == cfg.paper_trading


def test_telegram_controller_reads_the_same_flag(monkeypatch):
    """The status line must not be able to claim PAPER while execution is live."""
    import inspect
    from ui import telegram_controller

    src = inspect.getsource(telegram_controller)
    assert "config.trading" not in src or "paper_trading" not in src.split("config.trading")[1][:80], (
        "telegram_controller should read config.paper_trading, the flag execution uses"
    )
