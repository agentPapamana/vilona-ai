"""Operator CLI for the latched risk guards (D2 total drawdown, D3 consecutive losses).

D2 records `start_balance` once when a live phase begins and never recalculates
it. D3 counts contiguous losing Vilona trades. Both latches stay set until an
operator clears them with a recorded reason; nothing in the scanner clears them.

Examples:
  # Record the start balance for a new live phase (reads equity from the bridge)
  PYTHONPATH=. python scripts/risk_latches.py init-balance --source bridge

  # Record an explicit balance instead of reading the broker
  PYTHONPATH=. python scripts/risk_latches.py init-balance --value 100000

  # Show current guard state
  PYTHONPATH=. python scripts/risk_latches.py status

  # Clear both latches after reviewing the losses (reason is required)
  PYTHONPATH=. python scripts/risk_latches.py reset --reason "reviewed 4 losses, strategy intact"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_config
from core.risk_lock import consecutive_loss_count
from core.state_db import StateDB


def _bridge_equity() -> float | None:
    """Read equity through the configured bridge without placing any order."""
    from app_server import create_trading_bridge
    cfg = get_config()
    bridge = create_trading_bridge(cfg)
    if not bridge.connect():
        return None
    try:
        account = bridge.get_account_info()
    finally:
        disconnect = getattr(bridge, "disconnect", None)
        if callable(disconnect):
            disconnect()
    equity = getattr(account, "equity", None) if account else None
    return float(equity) if equity is not None else None


def _account_login() -> int:
    from app_server import create_trading_bridge
    cfg = get_config()
    bridge = create_trading_bridge(cfg)
    if not bridge.connect():
        return 0
    try:
        account = bridge.get_account_info()
    finally:
        disconnect = getattr(bridge, "disconnect", None)
        if callable(disconnect):
            disconnect()
    login = getattr(account, "login", 0) if account else 0
    try:
        return int(login)
    except (TypeError, ValueError):
        return 0


def cmd_init_balance(db: StateDB, args) -> int:
    if args.value is not None:
        balance = float(args.value)
        login = _account_login()
    else:
        balance = _bridge_equity()
        login = _account_login()
    if balance is None or balance <= 0:
        print("ERROR: could not read a positive equity from the bridge; "
              "pass --value to record it explicitly", file=sys.stderr)
        return 1
    db.set_start_balance(balance, account_login=login)
    print(f"start_balance recorded: {balance:.2f} (login {login})")
    print("Drawdown lock threshold (D2, "
          f"{get_config().trading.max_total_drawdown_pct:.2f}%): "
          f"{balance * (1 - get_config().trading.max_total_drawdown_pct / 100.0):.2f}")
    return 0


def cmd_status(db: StateDB, args) -> int:
    cfg = get_config()
    start = db.get_start_balance()
    print(f"start_balance:            {start}")
    print(f"start_balance_login:      {db.get_setting('start_balance_login')}")
    print(f"recorded_at:              {db.get_setting('start_balance_recorded_at')}")
    print(f"max_total_drawdown_pct:   {cfg.trading.max_total_drawdown_pct}")
    print(f"total_drawdown_latched:   {db.is_drawdown_latched()}")
    print(f"max_consecutive_losses:   {cfg.trading.max_consecutive_losses}")
    print(f"consecutive_loss_latched: {db.is_consecutive_loss_latched()}")
    try:
        reset_trade_id = int(db.get_setting("risk_latches_reset_trade_id") or 0)
    except (TypeError, ValueError):
        reset_trade_id = 0
    print(f"consecutive_loss_count:   {consecutive_loss_count(db.get_trades(limit=1000), after_trade_id=reset_trade_id)}")
    print(f"last_reset_reason:        {db.get_setting('risk_latches_reset_reason')}")
    print(f"last_reset_at:            {db.get_setting('risk_latches_reset_at')}")
    return 0


def cmd_reset(db: StateDB, args) -> int:
    if not args.reason.strip():
        print("ERROR: --reason is required; the reset is recorded in settings", file=sys.stderr)
        return 1
    db.reset_risk_latches(args.reason.strip())
    print(f"latches cleared: {args.reason.strip()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-balance", help="record start_balance for a new live phase")
    p_init.add_argument("--value", type=float, default=None,
                        help="explicit balance; default reads equity from the bridge")
    p_init.add_argument("--source", choices=["bridge", "value"], default="bridge",
                        help="accepted for symmetry; --value decides the source")
    p_init.set_defaults(func=cmd_init_balance)

    p_status = sub.add_parser("status", help="show guard state")
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset", help="clear both latches with a recorded reason")
    p_reset.add_argument("--reason", required=True, help="why the latches are cleared")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    db = StateDB()
    try:
        return args.func(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
