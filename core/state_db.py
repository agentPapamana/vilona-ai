"""SQLite persistence layer for Vilona AI v2."""
from __future__ import annotations
import json
import sqlite3
import time
import threading
from pathlib import Path
from typing import List, Optional, Dict, Any

from config import get_config, DatabaseConfig
from trading.bridge_ccxt import Position

class StateDB:
    def __init__(self, path: Optional[str] = None):
        cfg = get_config().database
        self.path = path or cfg.path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._connect()
        self._ensure_tables()

    def _connect(self):
        with self._lock:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    def _ensure_tables(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket INTEGER UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    current_sl REAL,
                    current_tp REAL,
                    status TEXT DEFAULT 'open',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket INTEGER UNIQUE,
                    symbol TEXT NOT NULL,
                    type TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL,
                    sl REAL,
                    tp REAL,
                    status TEXT DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    setup_key TEXT
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket INTEGER,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    sl REAL,
                    tp REAL,
                    profit REAL,
                    commission REAL,
                    swap REAL,
                    strategy TEXT,
                    reason TEXT,
                    opened_at REAL NOT NULL,
                    closed_at REAL,
                    status TEXT DEFAULT 'open'
                );
                CREATE TABLE IF NOT EXISTS daily_pl (
                    date TEXT PRIMARY KEY,
                    pl REAL NOT NULL,
                    wins INTEGER NOT NULL,
                    total_closed INTEGER NOT NULL,
                    equity REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trailing_state (
                    ticket INTEGER PRIMARY KEY,
                    symbol TEXT,
                    started_at_ms INTEGER NOT NULL DEFAULT 0,
                    started INTEGER NOT NULL DEFAULT 0,
                    last_sl REAL
                );
                CREATE TABLE IF NOT EXISTS judge_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    action TEXT,
                    confidence REAL,
                    risk_approved INTEGER,
                    lot_size REAL,
                    sl REAL,
                    tp REAL,
                    verdict TEXT,
                    reason TEXT,
                    latency_ms INTEGER,
                    error TEXT,
                    model TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_judge_decisions_symbol
                    ON judge_decisions(symbol, created_at);
                CREATE TABLE IF NOT EXISTS position_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket INTEGER,
                    symbol TEXT,
                    action TEXT NOT NULL,
                    old_sl REAL,
                    old_tp REAL,
                    requested_sl REAL,
                    requested_tp REAL,
                    result TEXT NOT NULL,
                    latency_ms INTEGER,
                    error TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_position_decisions_ticket
                    ON position_decisions(ticket, created_at);
                """)
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(orders)").fetchall()
            }
            if "setup_key" not in columns:
                self._conn.execute("ALTER TABLE orders ADD COLUMN setup_key TEXT")
            if "rejection_reason" not in columns:
                self._conn.execute("ALTER TABLE orders ADD COLUMN rejection_reason TEXT")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_setup_key "
                "ON orders(setup_key) WHERE setup_key IS NOT NULL"
            )
            # Trade rows are keyed by broker ticket: a ticket may close exactly
            # once, so re-detecting the same close must not duplicate the row.
            # Pre-existing rows written before this index are left NULL-ticketed
            # by the WHERE clause and are not deduplicated.
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_ticket "
                "ON trades(ticket) WHERE ticket IS NOT NULL"
            )
            # Existing paper-soak databases predate model attribution.
            columns = {
                row["name"] for row in self._conn.execute(
                    "PRAGMA table_info(judge_decisions)"
                ).fetchall()
            }
            if "model" not in columns:
                self._conn.execute("ALTER TABLE judge_decisions ADD COLUMN model TEXT")
            self._conn.commit()

    def save_position(self, pos: Position) -> int:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO positions
                   (ticket, symbol, side, quantity, entry_price, current_sl, current_tp, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (pos.ticket, pos.symbol, pos.type, pos.volume, pos.price_open, pos.sl, pos.tp, now, now)
            )
            self._conn.commit()
        return pos.ticket

    def update_sltp(self, ticket: int, sl: Optional[float], tp: Optional[float]):
        with self._lock:
            self._conn.execute(
                "UPDATE positions SET current_sl=?, current_tp=?, updated_at=? WHERE ticket=?",
                (sl, tp, time.time(), ticket)
            )
            self._conn.commit()

    def close_position(self, ticket: int):
        with self._lock:
            self._conn.execute(
                "UPDATE positions SET status='closed', updated_at=? WHERE ticket=?",
                (time.time(), ticket)
            )
            self._conn.commit()

    def get_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        with self._lock:
            if symbol:
                rows = self._conn.execute(
                    "SELECT * FROM positions WHERE symbol=? AND status='open'", (symbol,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM positions WHERE status='open'"
                ).fetchall()
            return [dict(r) for r in rows]

    def save_trailing_metadata(self, ticket: int, symbol: str, started_at_ms: int):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO trailing_state (ticket, symbol, started_at_ms, started) VALUES (?, ?, ?, 1)",
                (ticket, symbol, started_at_ms),
            )
            self._conn.commit()

    def get_trailing_metadata(self, ticket: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM trailing_state WHERE ticket=?", (ticket,)).fetchone()
            return dict(row) if row else None

    def prune_trailing_metadata(self, keep: set):
        with self._lock:
            if not keep:
                self._conn.execute("DELETE FROM trailing_state")
            else:
                placeholders = ",".join("?" for _ in keep)
                self._conn.execute(f"DELETE FROM trailing_state WHERE ticket NOT IN ({placeholders})", tuple(keep))
            self._conn.commit()

    def save_order(self, order: Dict[str, Any]) -> bool:
        """Record an order attempt; a non-null setup key is globally idempotent."""
        with self._lock:
            if not self._conn:
                return False
            try:
                self._conn.execute(
                    """INSERT INTO orders
                       (ticket, symbol, type, side, quantity, price, sl, tp, status, created_at, setup_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (order.get('ticket'), order.get('symbol'), order.get('type', 'market'),
                     order.get('side'), order.get('quantity'), order.get('price'),
                     order.get('sl'), order.get('tp'), order.get('status', 'pending'),
                     order.get('created_at') or time.time(), order.get('setup_key')),
                )
            except sqlite3.IntegrityError:
                return False
            self._conn.commit()
            return True

    def get_orders(self, limit: int = 100, since: float | None = None) -> List[Dict]:
        with self._lock:
            if not self._conn:
                return []
            if since is not None:
                rows = self._conn.execute(
                    "SELECT * FROM orders WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                    (float(since), limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
    def has_order_setup(self, setup_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM orders WHERE setup_key=? LIMIT 1", (setup_key,)
            ).fetchone()
            return row is not None
    def update_order_setup(
        self,
        setup_key: str,
        *,
        ticket: int | None,
        status: str,
        rejection_reason: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET ticket=?, status=?, "
                "rejection_reason=COALESCE(?, rejection_reason) WHERE setup_key=?",
                (ticket, status, rejection_reason, setup_key),
            )
            self._conn.commit()

    def count_recent_rejections(self, symbol: str, window_seconds: float = 3600.0) -> int:
        """Count broker rejections for one symbol inside a trailing window.

        The entry path uses this to stop resubmitting a symbol the broker keeps
        rejecting. Rows recorded but never sent carry another status and are not
        counted.
        """
        with self._lock:
            if not self._conn:
                return 0
            row = self._conn.execute(
                "SELECT COUNT(*) FROM orders "
                "WHERE symbol=? AND status='rejected' AND created_at >= ?",
                (symbol, time.time() - float(window_seconds)),
            ).fetchone()
            return int(row[0]) if row else 0

    def has_trade(self, ticket: int) -> bool:
        """Check if a trade row for this ticket already exists."""
        with self._lock:
            if not self._conn or ticket is None:
                return False
            row = self._conn.execute("SELECT 1 FROM trades WHERE ticket=? LIMIT 1", (ticket,)).fetchone()
            return row is not None

    def save_trade(self, trade: Dict[str, Any]) -> bool:
        """Save a trade record; ticket is idempotent so duplicates return False."""
        with self._lock:
            if not self._conn:
                return False
            ticket = trade.get('ticket')
            if ticket is not None and self._conn.execute("SELECT 1 FROM trades WHERE ticket=? LIMIT 1", (ticket,)).fetchone():
                return False
            try:
                self._conn.execute(
                    """INSERT INTO trades (ticket, symbol, side, quantity, entry_price, exit_price, sl, tp, profit, commission, swap, strategy, reason, opened_at, closed_at, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ticket, trade.get('symbol'), trade.get('side'), trade.get('quantity'), trade.get('entry_price'),
                     trade.get('exit_price'), trade.get('sl'), trade.get('tp'), trade.get('profit'), trade.get('commission'),
                     trade.get('swap'), trade.get('strategy'), trade.get('reason'), trade.get('opened_at'), trade.get('closed_at'), trade.get('status', 'closed')))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def save_position_decision(self, decision: Dict[str, Any]) -> None:
        """Record position-manager decisions and verification readback outcomes."""
        with self._lock:
            if not self._conn:
                return
            self._conn.execute(
                """INSERT INTO position_decisions (ticket, symbol, action, old_sl, old_tp, requested_sl, requested_tp, result, latency_ms, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (decision.get('ticket'), decision.get('symbol'), decision.get('action'), decision.get('old_sl'), decision.get('old_tp'),
                 decision.get('requested_sl'), decision.get('requested_tp'), decision.get('result', 'UNKNOWN'), decision.get('latency_ms'),
                 decision.get('error'), decision.get('created_at') or time.time()))
            self._conn.commit()

    def get_position_decisions(self, ticket: Optional[int] = None, limit: int = 100) -> List[Dict]:
        """Retrieve recorded position decisions for auditing."""
        with self._lock:
            if not self._conn:
                return []
            query = "SELECT * FROM position_decisions"
            params = []
            if ticket is not None:
                query += " WHERE ticket=?"
                params.append(ticket)
            query += " ORDER BY created_at DESC, id DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def get_trades(self, limit: int = 100) -> List[Dict]:
        """Retrieve recorded trades."""
        with self._lock:
            if not self._conn:
                return []
            return [dict(r) for r in self._conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def save_daily_pl(self, date: str, pl: float, wins: int, total: int, equity: float):
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO daily_pl 
                   (date, pl, wins, total_closed, equity, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (date, pl, wins, total, equity, time.time())
            )
            self._conn.commit()

    def get_daily_pl(self, date: Optional[str] = None) -> List[Dict]:
        with self._lock:
            if date:
                row = self._conn.execute(
                    "SELECT * FROM daily_pl WHERE date=?", (date,)
                ).fetchone()
                return [dict(row)] if row else []
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM daily_pl ORDER BY date DESC LIMIT 30"
            ).fetchall()]

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row['value'] if row else None

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
            self._conn.commit()
    def get_start_balance(self) -> float | None:
        raw = self.get_setting("start_balance")
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            return None

    def set_start_balance(self, balance: float, account_login: int = 0) -> None:
        self.set_setting("start_balance", str(float(balance)))
        self.set_setting("start_balance_login", str(int(account_login)))
        self.set_setting("start_balance_recorded_at", str(time.time()))

    def is_drawdown_latched(self) -> bool:
        return self.get_setting("total_drawdown_latched") == "true"

    def latch_drawdown(self, active: bool = True) -> None:
        self.set_setting("total_drawdown_latched", "true" if active else "false")

    def is_consecutive_loss_latched(self) -> bool:
        return self.get_setting("consecutive_loss_latched") == "true"

    def latch_consecutive_loss(self, active: bool = True) -> None:
        self.set_setting("consecutive_loss_latched", "true" if active else "false")

    def reset_risk_latches(self, reason: str = "") -> None:
        self.latch_drawdown(False)
        self.latch_consecutive_loss(False)
        self.set_setting("risk_latches_reset_reason", reason)
        self.set_setting("risk_latches_reset_at", str(time.time()))
        # Trades already reviewed by the operator are excluded from the next
        # streak by row id. A wall-clock cut-off is unusable here because the
        # broker reports deal time in server time (UTC+2), not host UTC.
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone() if self._conn else None
        self.set_setting("risk_latches_reset_trade_id", str(int(row[0]) if row else 0))

    def save_judge_decision(self, decision: Dict[str, Any]) -> None:
        """Record every judge outcome, including vetoes, timeouts and errors.

        An absent verdict means the judge never produced one (timeout, crash,
        unparseable response); that is stored as NULL rather than guessed.
        """
        with self._lock:
            if not self._conn:
                return
            self._conn.execute(
                """INSERT INTO judge_decisions
                   (symbol, action, confidence, risk_approved, lot_size, sl, tp,
                    verdict, reason, latency_ms, error, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (decision.get('symbol'), decision.get('action'),
                 decision.get('confidence'),
                 None if decision.get('risk_approved') is None
                 else int(bool(decision.get('risk_approved'))),
                 decision.get('lot_size'), decision.get('sl'), decision.get('tp'),
                 decision.get('verdict'), decision.get('reason'),
                 decision.get('latency_ms'), decision.get('error'),
                 decision.get('model'),
                 decision.get('created_at') or time.time())
            )
            self._conn.commit()

    def get_judge_decisions(self, limit: int = 100, since: float | None = None) -> List[Dict]:
        with self._lock:
            if not self._conn:
                return []
            if since is None:
                rows = self._conn.execute(
                    "SELECT * FROM judge_decisions ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM judge_decisions WHERE created_at >= ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (float(since), limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_judge_decision_stats(self, model: str | None = None) -> Dict[str, int]:
        """Counts used by the paper-soak evaluation; errors are their own bucket."""
        with self._lock:
            if not self._conn:
                return {"total": 0, "approved": 0, "vetoed": 0, "errors": 0, "no_verdict": 0}
            query = """SELECT
                     COUNT(*) AS total,
                     SUM(CASE WHEN verdict='APPROVE' THEN 1 ELSE 0 END) AS approved,
                     SUM(CASE WHEN verdict='VETO' THEN 1 ELSE 0 END) AS vetoed,
                     SUM(CASE WHEN verdict='ERROR' THEN 1 ELSE 0 END) AS errors,
                     SUM(CASE WHEN verdict IS NULL THEN 1 ELSE 0 END) AS no_verdict
                   FROM judge_decisions"""
            params = []
            if model is not None:
                query += " WHERE model = ?"
                params.append(model)
            row = self._conn.execute(query, params).fetchone()
            return {
                "total": int(row["total"] or 0),
                "approved": int(row["approved"] or 0),
                "vetoed": int(row["vetoed"] or 0),
                "errors": int(row["errors"] or 0),
                "no_verdict": int(row["no_verdict"] or 0),
            }

    def get_judge_decision_metrics(self, model: str | None = None) -> Dict[str, Dict[str, Any]]:
        """Return decision count, error rate and latency grouped by symbol."""
        with self._lock:
            if not self._conn:
                return {}
            query = """SELECT symbol,
                          COUNT(*) AS total,
                          SUM(CASE WHEN verdict='APPROVE' THEN 1 ELSE 0 END) AS approved,
                          SUM(CASE WHEN verdict='VETO' THEN 1 ELSE 0 END) AS vetoed,
                          SUM(CASE WHEN verdict='ERROR' THEN 1 ELSE 0 END) AS errors,
                          AVG(latency_ms) AS avg_latency_ms
                   FROM judge_decisions"""
            params = []
            if model is not None:
                query += " WHERE model = ?"
                params.append(model)
            query += " GROUP BY symbol ORDER BY symbol"
            rows = self._conn.execute(query, params).fetchall()
            result = {}
            for row in rows:
                total = int(row["total"] or 0)
                errors = int(row["errors"] or 0)
                result[row["symbol"]] = {
                    "total": total,
                    "approved": int(row["approved"] or 0),
                    "vetoed": int(row["vetoed"] or 0),
                    "errors": errors,
                    "error_rate": errors / total if total else 0.0,
                    "avg_latency_ms": float(row["avg_latency_ms"] or 0.0),
                }
            return result

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None