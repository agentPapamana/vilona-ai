"""State manager for short-term memory (memory.json) and episodic long-term state (scalper.json)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_PATH = "memory.json"
DEFAULT_SCALPER_DB_PATH = "scalper.json"
MAX_ROLLING_TRADES = 50
MAX_SPREAD_SAMPLES = 60


class ScalperStateManager:
    """Manages telemetry, short-term rolling metrics, and episodic state."""

    def __init__(
        self,
        memory_path: str = DEFAULT_MEMORY_PATH,
        scalper_path: str = DEFAULT_SCALPER_DB_PATH,
        max_trades: int = MAX_ROLLING_TRADES,
    ) -> None:
        self.memory_path = memory_path
        self.scalper_path = scalper_path
        self.max_trades = max_trades
        self._memory_data: Dict[str, Any] = self._load_json(
            self.memory_path,
            default={"recent_trades": [], "spread_telemetry": {}, "updated_at": 0},
        )
        self._scalper_data: Dict[str, Any] = self._load_json(
            self.scalper_path, default={"sessions": []}
        )

    @staticmethod
    def _load_json(path: str, default: Dict[str, Any]) -> Dict[str, Any]:
        if not os.path.exists(path):
            return dict(default)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else dict(default)
        except Exception as exc:
            logger.warning("Failed to read %s (%s). Using blank default state.", path, exc)
            return dict(default)

    def _save_json_atomic(self, path: str, data: Dict[str, Any]) -> bool:
        try:
            dir_name = os.path.dirname(os.path.abspath(path))
            os.makedirs(dir_name, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                json.dump(data, tf, indent=2)
                temp_path = tf.name
            os.replace(temp_path, path)
            return True
        except Exception as exc:
            logger.error("Failed writing atomic state to %s: %s", path, exc)
            return False

    def record_spread(self, symbol: str, spread_points: float) -> None:
        """Track running average, min, max spread for each symbol."""
        if spread_points <= 0:
            return
        telemetry = self._memory_data.setdefault("spread_telemetry", {})
        sym_stat = telemetry.setdefault(
            symbol, {"avg": spread_points, "min": spread_points, "max": spread_points, "samples": 0}
        )

        samples = int(sym_stat.get("samples", 0))
        old_avg = float(sym_stat.get("avg", spread_points))
        new_samples = min(samples + 1, MAX_SPREAD_SAMPLES)
        new_avg = old_avg + (spread_points - old_avg) / new_samples

        sym_stat["avg"] = round(new_avg, 4)
        sym_stat["min"] = round(min(float(sym_stat.get("min", spread_points)), spread_points), 4)
        sym_stat["max"] = round(max(float(sym_stat.get("max", spread_points)), spread_points), 4)
        sym_stat["current"] = round(spread_points, 4)
        sym_stat["samples"] = new_samples
        self._memory_data["updated_at"] = int(time.time())
        self._save_json_atomic(self.memory_path, self._memory_data)

    def record_trade_entry(
        self,
        ticket: int,
        symbol: str,
        action: str,
        open_price: float,
        sl: float,
        tp: float,
        spread: float,
    ) -> None:
        """Record trade entry telemetry."""
        trades = self._memory_data.setdefault("recent_trades", [])
        entry_record = {
            "ticket": ticket,
            "symbol": symbol,
            "action": action,
            "open_price": float(open_price),
            "close_price": None,
            "sl": float(sl),
            "tp": float(tp),
            "pnl_usd": 0.0,
            "spread_at_entry": float(spread),
            "slippage": 0.0,
            "duration_seconds": 0,
            "exit_reason": "OPEN",
            "be_triggered": False,
            "timestamp": int(time.time()),
        }
        trades.append(entry_record)
        if len(trades) > self.max_trades:
            self._memory_data["recent_trades"] = trades[-self.max_trades :]
        self._memory_data["updated_at"] = int(time.time())
        self._save_json_atomic(self.memory_path, self._memory_data)

    def record_be_triggered(self, ticket: int) -> None:
        """Mark trade as having activated micro-breakeven protection."""
        trades = self._memory_data.get("recent_trades", [])
        for t in reversed(trades):
            if t.get("ticket") == ticket:
                t["be_triggered"] = True
                self._save_json_atomic(self.memory_path, self._memory_data)
                break

    def record_trade_exit(
        self,
        ticket: int,
        close_price: float,
        pnl_usd: float,
        exit_reason: str,
        slippage: float = 0.0,
    ) -> None:
        """Update closed trade record with final outcome metrics."""
        trades = self._memory_data.get("recent_trades", [])
        now_ts = int(time.time())
        for t in reversed(trades):
            if t.get("ticket") == ticket:
                t["close_price"] = float(close_price)
                t["pnl_usd"] = round(float(pnl_usd), 4)
                t["exit_reason"] = exit_reason
                t["slippage"] = round(float(slippage), 4)
                open_ts = int(t.get("timestamp", now_ts))
                t["duration_seconds"] = max(0, now_ts - open_ts)
                break
        else:
            # If entry was recorded before restart or outside memory
            trades.append(
                {
                    "ticket": ticket,
                    "symbol": "UNKNOWN",
                    "action": "UNKNOWN",
                    "open_price": float(close_price),
                    "close_price": float(close_price),
                    "sl": 0.0,
                    "tp": 0.0,
                    "pnl_usd": round(float(pnl_usd), 4),
                    "spread_at_entry": 0.0,
                    "slippage": round(float(slippage), 4),
                    "duration_seconds": 0,
                    "exit_reason": exit_reason,
                    "be_triggered": False,
                    "timestamp": now_ts,
                }
            )

        if len(trades) > self.max_trades:
            self._memory_data["recent_trades"] = trades[-self.max_trades :]
        self._memory_data["updated_at"] = now_ts
        self._save_json_atomic(self.memory_path, self._memory_data)

    def get_short_term_context(self) -> Dict[str, Any]:
        """Produce compact telemetry summary for LLM prompt ingestion."""
        trades = self._memory_data.get("recent_trades", [])
        closed = [t for t in trades if t.get("close_price") is not None]
        wins = [t for t in closed if float(t.get("pnl_usd", 0.0)) > 0]
        losses = [t for t in closed if float(t.get("pnl_usd", 0.0)) < 0]

        total_closed = len(closed)
        win_rate = (len(wins) / total_closed) if total_closed > 0 else 0.0
        total_pnl = sum(float(t.get("pnl_usd", 0.0)) for t in closed)
        avg_dur = (
            sum(int(t.get("duration_seconds", 0)) for t in closed) / total_closed
            if total_closed > 0
            else 0
        )

        return {
            "total_recorded_trades": len(trades),
            "closed_trades_count": total_closed,
            "win_rate": round(win_rate, 2),
            "net_pnl_usd": round(total_pnl, 2),
            "avg_duration_seconds": round(avg_dur, 1),
            "spread_telemetry": self._memory_data.get("spread_telemetry", {}),
            "last_5_trades": closed[-5:] if closed else trades[-5:],
        }

    def save_session_summary(self, session_id: str, summary: Dict[str, Any]) -> None:
        """Persist long-term episodic record into scalper.json."""
        sessions = self._scalper_data.setdefault("sessions", [])
        entry = {"session_id": session_id, "timestamp": int(time.time()), **summary}
        sessions.append(entry)
        if len(sessions) > 50:
            self._scalper_data["sessions"] = sessions[-50:]
        self._save_json_atomic(self.scalper_path, self._scalper_data)

    def get_long_term_context(self) -> List[Dict[str, Any]]:
        """Return historical session summaries."""
        return self._scalper_data.get("sessions", [])[-5:]
