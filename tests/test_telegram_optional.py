"""Telegram alerting is optional; a live account can demand it explicitly (Opsi B).

Rationale: the demo canary has no Telegram credentials, and the alert channel is
not an entry guard — `TelegramController.start()` already returns after a warning
when the token is empty, and `QuantScanner` only calls `send_alert` when the
controller exposes it. Hard-blocking startup for every live-demo run therefore
removed no safety margin while preventing the canary from running at all.

The guard that mattered is kept: an operator running a real account sets
`KINGAI_TELEGRAM_REQUIRED=true`, and the process then refuses to start without a
token and at least one chat id.
"""
from __future__ import annotations

import pytest

from config import Config


def _live_config(monkeypatch) -> Config:
    monkeypatch.setenv("KINGAI_PAPER_TRADING", "false")
    monkeypatch.setenv("KINGAI_EXCHANGE", "mt5")
    monkeypatch.setenv("KINGAI_LLM_API_KEY", "test-key")
    monkeypatch.delenv("KINGAI_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KINGAI_TELEGRAM_CHAT_IDS", raising=False)
    monkeypatch.delenv("KINGAI_TELEGRAM_REQUIRED", raising=False)
    return Config.from_env()


def test_live_mode_without_telegram_is_valid_by_default(monkeypatch):
    cfg = _live_config(monkeypatch)
    assert cfg.paper_trading is False
    assert cfg.require_telegram is False
    assert cfg.telegram.bot_token == ""
    assert cfg.validate() == []


def test_require_telegram_flag_is_loaded_from_env(monkeypatch):
    cfg = _live_config(monkeypatch)
    monkeypatch.setenv("KINGAI_TELEGRAM_REQUIRED", "true")
    assert Config.from_env().require_telegram is True
    assert cfg.require_telegram is False


def test_missing_telegram_fails_when_required(monkeypatch):
    cfg = _live_config(monkeypatch)
    cfg.require_telegram = True
    errors = cfg.validate()
    assert "telegram.bot_token must be non-empty when KINGAI_TELEGRAM_REQUIRED=true" in errors
    assert "telegram.chat_ids must be non-empty list when KINGAI_TELEGRAM_REQUIRED=true" in errors


def test_configured_telegram_passes_when_required(monkeypatch):
    cfg = _live_config(monkeypatch)
    cfg.require_telegram = True
    cfg.telegram.bot_token = "123:abc"
    cfg.telegram.chat_ids = [42]
    assert cfg.validate() == []


def test_paper_mode_never_requires_telegram(monkeypatch):
    monkeypatch.setenv("KINGAI_PAPER_TRADING", "true")
    monkeypatch.setenv("KINGAI_TELEGRAM_REQUIRED", "true")
    monkeypatch.delenv("KINGAI_TELEGRAM_BOT_TOKEN", raising=False)
    cfg = Config.from_env()
    assert cfg.require_telegram is True
    # The guard only applies to live trading; paper mode must keep starting.
    assert [e for e in cfg.validate() if e.startswith("telegram.")] == []


def test_safety_log_reports_require_telegram(monkeypatch):
    """The startup header must show whether Telegram is mandatory."""
    from trading.bridge_mt5_mcp import MT5MCPBridge
    from core.quant_scanner import QuantScanner

    cfg = _live_config(monkeypatch)
    cfg.require_telegram = True
    scanner = QuantScanner(MT5MCPBridge("http://127.0.0.1:1", timeout=1), cfg, None, None)
    lines: list[str] = []
    monkeypatch.setattr(scanner, "_feed", lines.append)
    scanner.log_effective_safety_config()
    assert len(lines) == 1
    assert "require_telegram=True" in lines[0]
    assert "paper_trading=False" in lines[0]
