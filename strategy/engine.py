"""Standalone Native Execution Engine for scalp_5m_agresif.

Deterministic, zero-latency execution. 100% native Python code, zero AI runtime dependency.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional

from core.order_safety import MAGIC_BASE, place_protected_order, resolve_magic, resolve_min_stop_distance
from strategy.config import ScalpConfigFile, SymbolScalpConfig, load_scalp_config
from strategy.indicators import compute_m5_scalp_signal
from strategy.state_manager import ScalperStateManager

logger = logging.getLogger(__name__)

SCALP_MAGIC_OFFSET = 200


class ScalpAgresifEngine:
    """High-frequency 5m scalp execution engine with micro-BE and strict risk bounds."""

    def __init__(
        self,
        scanner: Any,
        config_path: str = "scalp.yaml",
        state_manager: Optional[ScalperStateManager] = None,
    ) -> None:
        self.scanner = scanner
        self.config_path = config_path
        self.state_manager = state_manager or ScalperStateManager()
        self._last_setup_key: Dict[str, str] = {}
        self._last_entry_time: Dict[str, float] = {}
        self._cached_config: Optional[ScalpConfigFile] = None
        self._last_config_load: float = 0.0
        self._last_fine_tune: Dict[str, float] = {}
        from strategy.fine_tuner import ScalpFineTuner
        tuner_model = getattr(getattr(self.scanner.cfg, "llm", None), "tuner_model", "antigravity-series") or "antigravity-series"
        self.fine_tuner = ScalpFineTuner(config_path=self.config_path, model_name=tuner_model)
    def get_config(self) -> ScalpConfigFile:
        """Cache config in memory with 5-second reload interval."""
        now = time.time()
        if self._cached_config is None or (now - self._last_config_load) > 5.0:
            self._cached_config = load_scalp_config(self.config_path)
            self._last_config_load = now
        return self._cached_config

    def _feed(self, msg: str) -> None:
        if self.scanner and hasattr(self.scanner, "_feed"):
            try:
                self.scanner._feed(f"[SCALP-5M] {msg}")
            except Exception:
                pass
        logger.info("[SCALP-5M] %s", msg)

    def _agent_feed(self, role: str, symbol: str, msg: str) -> None:
        if self.scanner and hasattr(self.scanner, "_agent_feed"):
            try:
                self.scanner._agent_feed(role, symbol, f"[SCALP-5M] {msg}")
            except Exception:
                pass

    def evaluate_symbol(self, symbol: str, route: Any, tick: Any) -> None:
        """Main 5m scalp symbol evaluation loop."""
        config_file = self.get_config()
        if not config_file.global_settings.active:
            self._feed(f"{symbol}: Scalp engine inactive in global_settings")
            return

        sym_cfg: SymbolScalpConfig = config_file.get_symbol_config(symbol)
        if not sym_cfg.enabled:
            return

        if tick is None or float(getattr(tick, "bid", 0.0) or 0.0) <= 0:
            return

        bid = float(tick.bid)
        ask = float(tick.ask)
        spread = max(0.0, ask - bid)

        # Telemetry: Record live spread to short-term memory
        self.state_manager.record_spread(symbol, spread)

        # 1. Spread Protection Gate (Strict absolute filter)
        if spread > sym_cfg.max_spread_points:
            self._feed(
                f"{symbol}: WAIT — spread {spread:.4f} exceeds max {sym_cfg.max_spread_points:.4f}"
            )
            return

        # 2. Check Core Invariant Guards
        # Emergency operator kill switch
        if self.scanner and hasattr(self.scanner, "_kill_switch"):
            if self.scanner._kill_switch.is_active():
                self._feed(f"ENTRY LOCK {symbol}: operator kill switch active")
                return

        # 3. Check Open Positions Count (Scalp constraint: max 1 concurrent position active at any time)
        paper_mode = bool(getattr(self.scanner.cfg, "paper_trading", False)) if self.scanner and hasattr(self.scanner, "cfg") else True
        if paper_mode:
            trades = self.state_manager._memory_data.get("recent_trades", [])
            open_paper = [t for t in trades if t.get("symbol") == symbol and t.get("close_price") is None and t.get("exit_reason") == "OPEN"]
            if len(open_paper) >= 1:
                return
        else:
            positions = route.get_positions() if hasattr(route, "get_positions") else []
            symbol_positions = [p for p in positions if getattr(p, "symbol", "") == symbol]
            if len(symbol_positions) >= 1:
                return
        # 4. Fetch Multi-Timeframe Candles
        c5m = route.get_candles(symbol, "5m", limit=60) if hasattr(route, "get_candles") else []
        c15m = route.get_candles(symbol, "15m", limit=60) if hasattr(route, "get_candles") else []
        c1h = route.get_candles(symbol, "1h", limit=60) if hasattr(route, "get_candles") else []

        if len(c5m) < 25 or len(c15m) < 30:
            self._feed(f"{symbol}: insufficient scalp candles (5m={len(c5m)}, 15m={len(c15m)})")
            return

        candle_time = c5m[-1].get("time") if c5m else 0
        if self.scanner and hasattr(self.scanner, "_publish_market_data"):
            try:
                self.scanner._publish_market_data(
                    symbol, tick, {"5m": c5m[-1], "15m": c15m[-1], "1h": c1h[-1] if c1h else {}}
                )
            except Exception:
                pass
        # 5. Evaluate Native M5 Scalp Signal (Deterministic, Zero AI)
        sig = compute_m5_scalp_signal(
            c5m=c5m,
            c15m=c15m,
            c1h=c1h,
            bid=bid,
            ask=ask,
            max_spread_points=sym_cfg.max_spread_points,
            min_confidence=sym_cfg.min_confidence,
            ema_fast_p=sym_cfg.indicators.ema_fast,
            ema_slow_p=sym_cfg.indicators.ema_slow,
            trend_filter_p=sym_cfg.indicators.trend_filter_ema,
            band_multiplier=sym_cfg.indicators.atr_multiplier_band,
            rsi_p=sym_cfg.indicators.rsi_period,
            rsi_ob=sym_cfg.indicators.rsi_overbought,
            rsi_os=sym_cfg.indicators.rsi_oversold,
        )

        if sig.action == "WAIT":
            # If setup was waiting due to extended pullback, trigger antigravity-series fine tuner
            now_ts = time.time()
            last_tuned = self._last_fine_tune.get(symbol, 0.0)
            if (now_ts - last_tuned) > 45.0:  # Tune at most once every 45s when market is moving fast
                dist_to_ema = abs(sig.ema_fast - bid) if sig.ema_fast > 0 else 0.0
                if dist_to_ema > (1.2 * sig.atr_5m) and sig.atr_5m > 0:
                    llm_cfg = getattr(self.scanner.cfg, "llm", None) if self.scanner else None
                    if llm_cfg and getattr(llm_cfg, "enabled", False):
                        try:
                            from llm.client import get_llm_client
                            tuner_model = getattr(llm_cfg, "tuner_model", "finetuner")
                            self._feed(f"[FINETUNER] Triggering model finetuner for {symbol} (dist={dist_to_ema:.2f}pts > band={sig.atr_5m*1.2:.2f}pts)...")
                            self._agent_feed("finetuner", symbol, f"CALIBRATE — Optimizing execution triggers via model finetuner")
                            tuned = self.fine_tuner.tune_symbol(
                                symbol=symbol,
                                current_tick=tick,
                                candles_5m=c5m,
                                atr_5m=sig.atr_5m,
                                dist_to_ema=dist_to_ema,
                                client=get_llm_client(),
                                timeout_s=15.0,
                            )
                            if tuned:
                                self._cached_config = None
                                sym_c = self.get_config().get_symbol_config(symbol)
                                self._feed(f"[FINETUNER] Applied on-the-fly: TP={sym_c.tp_points}pts, SL={sym_c.sl_points}pts, Band={sym_c.indicators.atr_multiplier_band}x, MinConf={sym_c.min_confidence}")
                                self._agent_feed("finetuner", symbol, f"APPLIED — TP={sym_c.tp_points} SL={sym_c.sl_points} Band={sym_c.indicators.atr_multiplier_band}x")
                        except Exception as exc:
                            logger.debug("Background fine-tune attempt failed: %s", exc)
            return

        # If the previous trade has finished and closed (TP or SL hit), allow re-entry on new tick setup!
        # 6. S/L and T/P Sizing (Enforcing S/L <= 0.5% and T/P 1-3 points)
        symbol_info = route.get_symbol_info(symbol) if hasattr(route, "get_symbol_info") else None
        digits = getattr(symbol_info, "digits", None)
        try:
            digits = int(digits) if digits is not None else None
        except (TypeError, ValueError):
            digits = None

        trading_cfg = getattr(self.scanner.cfg, "trading", None) if self.scanner and hasattr(self.scanner, "cfg") else None
        broker_min_stop = resolve_min_stop_distance(symbol, trading_cfg)

        # SL points: bounded by config (e.g. 10.0 points) and broker min stop
        sl_points = max(sym_cfg.sl_points, broker_min_stop)
        # TP points: thin profit capture 1-3 points
        tp_points = max(1.0, min(3.0, sym_cfg.tp_points))

        entry_price = ask if sig.action == "BUY" else bid
        if sig.action == "BUY":
            sl_price = entry_price - sl_points
            tp_price = entry_price + tp_points
        else:
            sl_price = entry_price + sl_points
            tp_price = entry_price - tp_points

        if digits is not None:
            sl_price = round(sl_price, digits)
            tp_price = round(tp_price, digits)

        # 7. Account Equity & Micro Lot Sizing
        equity = 1000.0
        acc_info = route.get_account_info() if hasattr(route, "get_account_info") else None
        if acc_info and getattr(acc_info, "equity", None) is not None:
            equity = float(acc_info.equity)

        lot_min = float(getattr(symbol_info, "lot_min", 0.01) or 0.01)
        lot_step = float(getattr(symbol_info, "lot_step", 0.01) or 0.01)
        lot_max = float(getattr(symbol_info, "lot_max", 50.0) or 50.0)
        max_lot_cap = float(getattr(trading_cfg, "max_lot", 50.0) or 50.0)

        if sym_cfg.use_fixed_lot:
            lot_size = sym_cfg.fixed_lot
        else:
            # Enforce max 0.5% risk
            max_risk_usd = equity * (min(0.5, sym_cfg.risk_per_trade_pct) / 100.0)
            contract_mult = 100.0 if "XAU" in symbol.upper() else 100000.0
            calc_lot = max_risk_usd / (sl_points * contract_mult)
            steps = round(calc_lot / lot_step)
            lot_size = max(lot_min, steps * lot_step)

        lot_size = min(lot_size, lot_max, max_lot_cap)
        lot_size = round(lot_size, 2)

        # 8. Order Execution
        paper_trading = bool(getattr(self.scanner.cfg, "paper_trading", False)) if self.scanner and hasattr(self.scanner, "cfg") else True
        magic_val = resolve_magic(symbol, base=MAGIC_BASE + SCALP_MAGIC_OFFSET)

        self._feed(
            f"EXECUTE {sig.action} {symbol}: lot={lot_size} SL={sl_price} TP={tp_price} (spread={spread:.3f})"
        )

        if paper_trading:
            # Paper execution simulation
            mock_ticket = int(time.time() * 1000) % 100000000
            self.state_manager.record_trade_entry(
                ticket=mock_ticket,
                symbol=symbol,
                action=sig.action,
                open_price=entry_price,
                sl=sl_price,
                tp=tp_price,
                spread=spread,
            )
            self._last_setup_key[symbol] = f"{symbol}|{sig.action}|{candle_time}|{mock_ticket}"
            self._last_entry_time[symbol] = time.time()
            self._feed(f"PAPER FILLED {symbol}: ticket={mock_ticket} lot={lot_size}")
            return

        # Real broker execution via protected order
        res = place_protected_order(
            bridge=route,
            symbol=symbol,
            action=sig.action,
            volume=lot_size,
            sl=sl_price,
            tp=tp_price,
            magic=magic_val,
            comment="scalp-5m-agresif",
            reference_price=entry_price,
            min_stop_distance=broker_min_stop,
            digits=digits,
        )

        if res.success:
            self._last_setup_key[symbol] = f"{symbol}|{sig.action}|{candle_time}|{res.ticket}"
            self._last_entry_time[symbol] = time.time()
            ticket = getattr(res, "ticket", 0) or int(time.time())
            self.state_manager.record_trade_entry(
                ticket=ticket,
                symbol=symbol,
                action=sig.action,
                open_price=entry_price,
                sl=sl_price,
                tp=tp_price,
                spread=spread,
            )
            self._feed(f"LIVE FILLED {symbol}: ticket={ticket} protected=True")
        else:
            self._feed(f"ORDER FAILED {symbol}: {res.message}")

    def manage_open_positions(self, route: Any) -> None:
        """Micro-Breakeven and Position Outcome Manager for both Live and Paper mode."""
        paper_trading = bool(getattr(self.scanner.cfg, "paper_trading", False)) if self.scanner and hasattr(self.scanner, "cfg") else True
        if paper_trading:
            self._manage_paper_positions(route)
            return

        positions = route.get_positions() if hasattr(route, "get_positions") else []
        if not positions:
            return

        config_file = self.get_config()
        for pos in positions:
            symbol = getattr(pos, "symbol", "")
            ticket = getattr(pos, "ticket", 0)
            magic = getattr(pos, "magic", 0)

            # Check if owned by scalp strategy
            expected_magic = resolve_magic(symbol, base=MAGIC_BASE + SCALP_MAGIC_OFFSET)
            if magic != expected_magic and (magic < MAGIC_BASE or magic >= MAGIC_BASE + 1000):
                continue

            sym_cfg = config_file.get_symbol_config(symbol)
            be_points = sym_cfg.micro_be_points
            entry = float(getattr(pos, "price_open", 0.0) or 0.0)
            cur_price = float(getattr(pos, "price_current", 0.0) or 0.0)
            cur_sl = float(getattr(pos, "sl", 0.0) or 0.0)
            cur_tp = float(getattr(pos, "tp", 0.0) or 0.0)
            pos_type = str(getattr(pos, "type", "")).upper()

            is_buy = "BUY" in pos_type or getattr(pos, "type", None) == 0
            direction = 1.0 if is_buy else -1.0
            profit_points = (cur_price - entry) * direction

            # If profit meets or exceeds micro-BE threshold (e.g. +1.0 point)
            if profit_points >= be_points:
                target_be_sl = entry + (direction * 0.1)
                already_be = (cur_sl >= target_be_sl) if is_buy else (cur_sl <= target_be_sl and cur_sl > 0)
                if not already_be:
                    symbol_info = route.get_symbol_info(symbol) if hasattr(route, "get_symbol_info") else None
                    digits = getattr(symbol_info, "digits", None)
                    if digits is not None:
                        try:
                            target_be_sl = round(target_be_sl, int(digits))
                        except Exception:
                            pass

                    self._feed(
                        f"MICRO-BE ACTIVATED {symbol} ticket={ticket}: profit={profit_points:.2f}pts -> Move SL to {target_be_sl}"
                    )
                    try:
                        modify_fn = getattr(route, "modify_position", None)
                        if callable(modify_fn):
                            modify_fn(ticket=ticket, symbol=symbol, sl=target_be_sl, tp=cur_tp)
                            self.state_manager.record_be_triggered(ticket)
                    except Exception as exc:
                        logger.error("Failed to apply micro-BE to ticket %s: %s", ticket, exc)

    def _manage_paper_positions(self, route: Any) -> None:
        """Monitor paper-trading positions against live market price for TP, SL, and Micro-BE."""
        trades = self.state_manager._memory_data.get("recent_trades", [])
        open_paper_trades = [t for t in trades if t.get("close_price") is None and t.get("exit_reason") == "OPEN"]
        if not open_paper_trades:
            return

        for t in open_paper_trades:
            sym = t.get("symbol", "")
            ticket = t.get("ticket")
            tick = route.get_tick(sym) if hasattr(route, "get_tick") else None
            if not tick or float(getattr(tick, "bid", 0.0) or 0.0) <= 0:
                continue

            bid = float(tick.bid)
            ask = float(tick.ask)
            action = t.get("action", "BUY")
            entry_px = float(t.get("open_price", 0.0))
            sl_px = float(t.get("sl", 0.0))
            tp_px = float(t.get("tp", 0.0))
            is_buy = action == "BUY"

            # For a SELL, you buy back to cover at ASK. But the charts and market reach are measured by BID.
            # To close a SELL at TP, BID reaching tp_px is the standard trigger.
            cur_px = bid if not is_buy else ask
            direction = 1.0 if is_buy else -1.0
            profit_points = (entry_px - cur_px) if not is_buy else (cur_px - entry_px)

            config_file = self.get_config()
            sym_cfg = config_file.get_symbol_config(sym)
            be_thresh = sym_cfg.micro_be_points

            # 1. Micro-Breakeven Check
            if profit_points >= be_thresh and not t.get("be_triggered", False):
                new_sl = entry_px + (direction * 0.1)
                t["sl"] = new_sl
                self.state_manager.record_be_triggered(ticket)
                self._feed(f"PAPER MICRO-BE ACTIVATED {sym} ticket={ticket}: profit={profit_points:.2f}pts -> Move SL to {new_sl:.2f}")
                sl_px = new_sl

            # 2. Take Profit Check
            tp_hit = (cur_px >= tp_px) if is_buy else (cur_px <= tp_px and tp_px > 0)
            if tp_hit:
                pnl_pts = abs(tp_px - entry_px)
                pnl_usd = round(pnl_pts * 0.01 * 100.0, 4)
                self.state_manager.record_trade_exit(
                    ticket=ticket,
                    close_price=tp_px,
                    pnl_usd=pnl_usd,
                    exit_reason="TP_HIT",
                )
                self._record_to_db(t, close_price=tp_px, profit=pnl_usd, reason="TP_HIT")
                self._feed(f"PAPER TP HIT {sym} ticket={ticket}: exit={tp_px} profit=+{pnl_pts:.2f}pts (+${pnl_usd:.2f})")
                continue

            # 3. Stop Loss Check
            sl_hit = (cur_px <= sl_px) if is_buy else (cur_px >= sl_px and sl_px > 0)
            if sl_hit:
                loss_pts = abs(entry_px - sl_px)
                loss_usd = round(-loss_pts * 0.01 * 100.0, 4)
                self.state_manager.record_trade_exit(
                    ticket=ticket,
                    close_price=sl_px,
                    pnl_usd=loss_usd,
                    exit_reason="SL_HIT",
                )
                self._record_to_db(t, close_price=sl_px, profit=loss_usd, reason="SL_HIT")
                self._feed(f"PAPER SL HIT {sym} ticket={ticket}: exit={sl_px} pnl=-{loss_pts:.2f}pts (${loss_usd:.2f})")

    def _record_to_db(self, trade_record: Dict[str, Any], close_price: float, profit: float, reason: str) -> None:
        """Persist paper closed trade to StateDB so Dashboard reflects closed trade performance."""
        db = getattr(self.scanner, "db", None) if self.scanner else None
        if not db or not hasattr(db, "save_trade"):
            return
        try:
            ticket = int(trade_record.get("ticket", 0) or 0)
            if hasattr(db, "has_trade") and db.has_trade(ticket):
                return
            now_epoch = time.time()
            open_epoch = float(trade_record.get("timestamp", now_epoch) or now_epoch)
            db.save_trade({
                "ticket": ticket,
                "symbol": trade_record.get("symbol", "XAUUSDc"),
                "side": trade_record.get("action", "BUY"),
                "quantity": 0.01,
                "entry_price": float(trade_record.get("open_price", 0.0) or 0.0),
                "exit_price": float(close_price),
                "sl": float(trade_record.get("sl", 0.0) or 0.0),
                "tp": float(trade_record.get("tp", 0.0) or 0.0),
                "profit": float(profit),
                "commission": 0.0,
                "swap": 0.0,
                "strategy": "scalp_5m_agresif",
                "reason": reason,
                "opened_at": open_epoch,
                "closed_at": now_epoch,
                "status": "closed",
                "spread": float(trade_record.get("spread_at_entry", 0.0) or 0.0),
                "atr": 0.0,
            })
        except Exception as exc:
            logger.debug("Failed saving paper closed trade to StateDB: %s", exc)
