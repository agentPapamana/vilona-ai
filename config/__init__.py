"""
Vilona AI v2 Configuration Module

Centralized configuration management with environment variable overrides.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict
import json

def _load_env_file():
    """Load .env file into os.environ if present, without requiring external dotenv package."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                # Strip inline comments for clean unquoted values
                v = v.split("#")[0].strip()
                v = v.strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass

_load_env_file()
from typing import Optional, List, Dict
import json


# Hard ceiling on stacked positions per symbol. Each entry is still bounded by
# max_lot, the daily-loss lock and the drawdown latch; this only stops a typo
# (e.g. 1000) from opening an unbounded book.
MAX_POSITIONS_PER_SYMBOL_LIMIT = 100

@dataclass
class ExchangeConfig:
    """Exchange connection configuration."""
    name: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    password: str = ""
    sandbox: bool = False
    rate_limit: int = 1200
    # CCXT market type: spot for balances or swap for linear perpetual positions.
    market_type: str = "spot"
    # MT5 transport: "package" = local MetaTrader5 Python package (Windows only)
    #                "mcp"     = external MT5 MCP server (works on any OS)
    mt5_transport: str = "mcp"
    mt5_mcp_url: str = "http://127.0.0.1:8765"
    mt5_mcp_timeout: int = 30
    mt5_client_token: str = ""

@dataclass
class TelegramConfig:
    """Telegram bot configuration"""
    bot_token: str = ""
    chat_ids: List[int] = field(default_factory=list)
    admin_chat_ids: List[int] = field(default_factory=list)
    polling_interval: float = 1.0


@dataclass
class LLMConfig:
    """LLM/Agent configuration"""
    provider: str = "openrouter"
    api_key: str = ""
    base_url: Optional[str] = None
    model: str = "anthropic/claude-3.5-sonnet"
    tuner_model: str = "antigravity-series"
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout: int = 30
    enabled: bool = False
    require_approve: bool = True


@dataclass
class TradingConfig:
    """Trading engine configuration"""
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    timeframes: List[str] = field(default_factory=lambda: ["1m", "5m", "15m", "1h"])
    scan_interval: int = 30

    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_total_drawdown_pct: float = 10.0
    max_consecutive_losses: int = 4
    max_concurrent_positions: int = 5
    max_tick_age_seconds: float = 30.0
    bridge_heartbeat_timeout_s: float = 60.0
    weekend_entry_cutoff_min: int = 120
    weekend_flatten_min: int = 60
    week_open_grace_min: int = 30
    max_positions_per_symbol: int = 1
    max_lot: float = 50.0
    min_lot: float = 0.01
    lot_step: float = 0.01
    # Keep standard XAUUSD at its 10.0 safety buffer. The broker's cent
    # contract XAUUSDc accepted 5.0 in live validation; keep that rule scoped
    # to the cent symbol instead of weakening the standard symbol.
    min_stop_distance: float = 0.0
    min_stop_distance_by_symbol: Dict[str, float] = field(
        default_factory=lambda: {"XAUUSDc": 5.0, "XAUUSD": 10.0, "GOLD": 10.0, "XAGUSD": 0.5}
    )
    max_spread_pct: float = 0.1
    symbol_cooldown_seconds: int = 300
    min_confidence: float = 0.55
    max_spread_pct_default: float = 0.15
    # Scalping must have enough movement to overcome the live spread.
    scalp_max_spread_atr: float = 0.35
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    min_volume_usd: float = 100.0

    be_trigger_atr_mult: float = 1.5
    be_offset_atr_mult: float = 0.5
    trailing_atr_mult: float = 2.0
    trailing_step_atr_mult: float = 0.5
    # Deterministic position protection runs independently of the LLM manager.
    emergency_loss_atr_mult: float = 2.0
    emergency_stop_atr_mult: float = 1.5
    emergency_close_on_loss: bool = True

    max_drift_points: int = 100
    max_drift_candles: int = 12
    drift_timeframe: str = "15m"
    strategy: str = "pullback"
    breakout_lookback: int = 20
    scalp_pullback_band: float = 1.5

    paper_trading: bool = True


@dataclass
class DatabaseConfig:
    """Database configuration"""
    path: str = "vilona_v2.db"


@dataclass
class BuildConfig:
    """Build configuration for packaging"""
    version: str = "Vilona AI-v2.2"
    icon_path: Optional[str] = None


@dataclass
class JudgeBreakerConfig:
    """Circuit breaker for LLM judge calls."""
    failure_threshold: int = 3
    cooldown_seconds: float = 120.0


@dataclass
class Config:
    """Main configuration container"""
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    judge_breaker: JudgeBreakerConfig = field(default_factory=JudgeBreakerConfig)
    headless: bool = True
    paper_trading: bool = True
    debug: bool = False
    log_level: str = "INFO"
    require_telegram: bool = False

    enabled_personas: List[str] = field(default_factory=lambda: ["trader", "risk_manager"])

    systemone_enabled: bool = False
    systemone_endpoint: Optional[str] = None
    systemone_api_key: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables"""
        config = cls()

        config.exchange.name = os.getenv("KINGAI_EXCHANGE", config.exchange.name)
        config.exchange.api_key = os.getenv("KINGAI_EXCHANGE_API_KEY", config.exchange.api_key)
        config.exchange.api_secret = os.getenv("KINGAI_EXCHANGE_API_SECRET", config.exchange.api_secret)
        config.exchange.password = os.getenv("KINGAI_EXCHANGE_PASSWORD", config.exchange.password)
        config.exchange.sandbox = os.getenv("KINGAI_EXCHANGE_SANDBOX", "false").lower() == "true"
        market_type = os.getenv("KINGAI_EXCHANGE_TYPE", config.exchange.market_type).strip().lower()
        if market_type == "futures":
            market_type = "future"
        if market_type not in {"spot", "swap", "future"}:
            raise ValueError("KINGAI_EXCHANGE_TYPE must be one of 'spot', 'swap', or 'future'")
        config.exchange.market_type = market_type
        config.exchange.mt5_transport = os.getenv("KINGAI_MT5_TRANSPORT", config.exchange.mt5_transport)
        config.exchange.mt5_mcp_url = os.getenv("KINGAI_MT5_MCP_URL", config.exchange.mt5_mcp_url)
        config.exchange.mt5_mcp_timeout = int(
            os.getenv("KINGAI_MT5_MCP_TIMEOUT", str(config.exchange.mt5_mcp_timeout))
        )
        config.exchange.mt5_client_token = os.getenv("MT5_CLIENT_TOKEN", os.getenv("KINGAI_MT5_CLIENT_TOKEN", config.exchange.mt5_client_token))
        # Default false so a demo canary can start with Telegram disabled.
        config.require_telegram = os.getenv("KINGAI_TELEGRAM_REQUIRED", "false").lower() == "true"

        config.telegram.bot_token = os.getenv("KINGAI_TELEGRAM_BOT_TOKEN", config.telegram.bot_token)
        chat_ids_str = os.getenv("KINGAI_TELEGRAM_CHAT_IDS", "")
        if chat_ids_str:
            config.telegram.chat_ids = [int(x.strip()) for x in chat_ids_str.split(",")]
        admin_ids_str = os.getenv("KINGAI_TELEGRAM_ADMIN_IDS", "")
        if admin_ids_str:
            config.telegram.admin_chat_ids = [int(x.strip()) for x in admin_ids_str.split(",")]

        config.llm.provider = os.getenv("KINGAI_LLM_PROVIDER", config.llm.provider)
        config.llm.api_key = os.getenv("KINGAI_LLM_API_KEY", config.llm.api_key)
        config.llm.base_url = os.getenv("KINGAI_LLM_BASE_URL", config.llm.base_url)
        config.llm.model = os.getenv("KINGAI_LLM_MODEL", config.llm.model)
        config.llm.tuner_model = os.getenv("KINGAI_LLM_TUNER_MODEL", config.llm.tuner_model)
        config.llm.enabled = os.getenv("KINGAI_LLM_ENABLED", "false").lower() == "true"
        config.llm.require_approve = os.getenv("KINGAI_LLM_REQUIRE_APPROVE", "true").lower() == "true"
        timeout_raw = os.getenv("KINGAI_LLM_TIMEOUT")
        if timeout_raw:
            config.llm.timeout = int(float(timeout_raw))
            if config.llm.timeout <= 0:
                raise ValueError("KINGAI_LLM_TIMEOUT must be positive")
        config.trading.max_concurrent_positions = int(os.getenv("KINGAI_MAX_CONCURRENT_POSITIONS", str(config.trading.max_concurrent_positions)))
        if config.trading.max_concurrent_positions <= 0:
            raise ValueError("KINGAI_MAX_CONCURRENT_POSITIONS must be positive")
        per_symbol_raw = os.getenv("KINGAI_MAX_POSITIONS_PER_SYMBOL")
        if per_symbol_raw:
            config.trading.max_positions_per_symbol = int(per_symbol_raw)
            if not 1 <= config.trading.max_positions_per_symbol <= MAX_POSITIONS_PER_SYMBOL_LIMIT:
                raise ValueError(
                    f"KINGAI_MAX_POSITIONS_PER_SYMBOL must be between 1 and {MAX_POSITIONS_PER_SYMBOL_LIMIT}"
                )
        cooldown_raw = os.getenv("KINGAI_SYMBOL_COOLDOWN_SECONDS")
        if cooldown_raw:
            config.trading.symbol_cooldown_seconds = int(cooldown_raw)
            if config.trading.symbol_cooldown_seconds < 0:
                raise ValueError("KINGAI_SYMBOL_COOLDOWN_SECONDS must be >= 0")
        lot_raw = os.getenv("KINGAI_MAX_LOT")
        if lot_raw is not None:
            config.trading.max_lot = float(lot_raw)
            if config.trading.max_lot <= 0:
                raise ValueError("KINGAI_MAX_LOT must be positive")
        strategy_raw = os.getenv("KINGAI_STRATEGY")
        if strategy_raw:
            config.trading.strategy = strategy_raw.strip().lower()
        scalp_band_raw = os.getenv("KINGAI_SCALP_PULLBACK_BAND")
        if scalp_band_raw:
            config.trading.scalp_pullback_band = float(scalp_band_raw)
        spread_atr_raw = os.getenv("KINGAI_SCALP_MAX_SPREAD_ATR")
        if spread_atr_raw:
            config.trading.scalp_max_spread_atr = float(spread_atr_raw)
            if config.trading.scalp_max_spread_atr <= 0:
                raise ValueError("KINGAI_SCALP_MAX_SPREAD_ATR must be positive")
        breakout_raw = os.getenv("KINGAI_BREAKOUT_LOOKBACK")
        if breakout_raw:
            config.trading.breakout_lookback = int(breakout_raw)
            if config.trading.breakout_lookback < 2:
                raise ValueError("KINGAI_BREAKOUT_LOOKBACK must be at least 2")
        sl_mult_raw = os.getenv("KINGAI_SL_ATR_MULT")
        if sl_mult_raw:
            config.trading.sl_atr_mult = float(sl_mult_raw)
            if config.trading.sl_atr_mult <= 0:
                raise ValueError("KINGAI_SL_ATR_MULT must be positive")
        tp_mult_raw = os.getenv("KINGAI_TP_ATR_MULT")
        if tp_mult_raw:
            config.trading.tp_atr_mult = float(tp_mult_raw)
            if config.trading.tp_atr_mult <= 0:
                raise ValueError("KINGAI_TP_ATR_MULT must be positive")
        config.judge_breaker.failure_threshold = int(
            os.getenv("KINGAI_JUDGE_BREAKER_FAILURE_THRESHOLD",
                      str(config.judge_breaker.failure_threshold))
        )
        config.judge_breaker.cooldown_seconds = float(
            os.getenv("KINGAI_JUDGE_BREAKER_COOLDOWN_SECONDS",
                      str(config.judge_breaker.cooldown_seconds))
        )

        symbols_str = os.getenv("KINGAI_SYMBOLS", "")
        if symbols_str:
            config.trading.symbols = [x.strip() for x in symbols_str.split(",")]

        timeframes_str = os.getenv("KINGAI_TIMEFRAMES", "")
        if timeframes_str:
            config.trading.timeframes = [x.strip() for x in timeframes_str.split(",")]

        config.trading.scan_interval = int(os.getenv("KINGAI_SCAN_INTERVAL", str(config.trading.scan_interval)))
        config.trading.max_risk_per_trade_pct = float(os.getenv("KINGAI_MAX_RISK_PCT", str(config.trading.max_risk_per_trade_pct)))
        config.trading.max_daily_loss_pct = float(os.getenv("KINGAI_MAX_DAILY_LOSS_PCT", str(config.trading.max_daily_loss_pct)))
        config.trading.max_total_drawdown_pct = float(os.getenv("KINGAI_MAX_TOTAL_DRAWDOWN_PCT", str(config.trading.max_total_drawdown_pct)))
        config.trading.max_consecutive_losses = int(os.getenv("KINGAI_MAX_CONSECUTIVE_LOSSES", str(config.trading.max_consecutive_losses)))
        config.trading.max_tick_age_seconds = float(os.getenv("KINGAI_MAX_TICK_AGE_SECONDS", str(config.trading.max_tick_age_seconds)))
        config.trading.weekend_entry_cutoff_min = int(os.getenv("KINGAI_WEEKEND_ENTRY_CUTOFF_MIN", str(config.trading.weekend_entry_cutoff_min)))
        config.trading.weekend_flatten_min = int(os.getenv("KINGAI_WEEKEND_FLATTEN_MIN", str(config.trading.weekend_flatten_min)))
        config.trading.week_open_grace_min = int(os.getenv("KINGAI_WEEK_OPEN_GRACE_MIN", str(config.trading.week_open_grace_min)))
        # Single source of truth: the scanner, dashboard, validator and dirty-check
        # all read `config.paper_trading`. `trading.paper_trading` is kept as a
        # mirror for older callers but is written from the same value so the two
        # can never disagree.
        paper = os.getenv("KINGAI_PAPER_TRADING", "true").lower() == "true"
        config.paper_trading = paper
        config.trading.paper_trading = paper

        config.headless = os.getenv("KINGAI_HEADLESS", "true").lower() == "true"
        config.debug = os.getenv("KINGAI_DEBUG", "false").lower() == "true"
        config.log_level = os.getenv("KINGAI_LOG_LEVEL", config.log_level)

        personas_str = os.getenv("KINGAI_PERSONAS", "")
        if personas_str:
            config.enabled_personas = [x.strip() for x in personas_str.split(",")]

        config.systemone_enabled = os.getenv("KINGAI_SYSTEMONE_ENABLED", "false").lower() == "true"
        config.systemone_endpoint = os.getenv("KINGAI_SYSTEMONE_ENDPOINT", config.systemone_endpoint)
        config.systemone_api_key = os.getenv("KINGAI_SYSTEMONE_API_KEY", config.systemone_api_key)

        return config

    def validate(self) -> List[str]:
        """Validate configuration and return list of error strings."""
        errors = []

        if not self.paper_trading:
            if self.exchange.name.lower() != 'mt5':
                if not self.exchange.api_key:
                    errors.append("paper_trading=False requires exchange.api_key")
                if not self.exchange.api_secret:
                    errors.append("paper_trading=False requires exchange.api_secret")
            # Telegram alerting is OPTIONAL by default. A missing bot token only
            # disables outbound alerts (`TelegramController.start()` returns after
            # a warning), it does not weaken any entry guard. Operators running a
            # real account MUST set `KINGAI_TELEGRAM_REQUIRED=true` so the process
            # refuses to start without an alert channel.
            if self.require_telegram:
                if not self.telegram.bot_token:
                    errors.append("telegram.bot_token must be non-empty when KINGAI_TELEGRAM_REQUIRED=true")
                if not self.telegram.chat_ids:
                    errors.append("telegram.chat_ids must be non-empty list when KINGAI_TELEGRAM_REQUIRED=true")
            if not self.llm.api_key:
                errors.append("llm.api_key must be non-empty")

        if not self.trading.symbols:
            errors.append("trading.symbols must be non-empty")
        if self.trading.max_risk_per_trade_pct <= 0 or self.trading.max_risk_per_trade_pct > 10:
            errors.append("trading.max_risk_per_trade_pct must be > 0 and <= 10")
        if self.trading.max_daily_loss_pct <= 0 or self.trading.max_daily_loss_pct > 50:
            errors.append("trading.max_daily_loss_pct must be > 0 and <= 50")
        if self.trading.max_total_drawdown_pct <= 0 or self.trading.max_total_drawdown_pct > 50:
            errors.append("trading.max_total_drawdown_pct must be > 0 and <= 50")
        if self.trading.max_consecutive_losses <= 0:
            errors.append("trading.max_consecutive_losses must be > 0")
        if self.trading.max_tick_age_seconds <= 0:
            errors.append("trading.max_tick_age_seconds must be > 0")
        if self.trading.weekend_entry_cutoff_min < 0 or self.trading.weekend_entry_cutoff_min > 1440:
            errors.append("trading.weekend_entry_cutoff_min must be >= 0 and <= 1440")
        if self.trading.weekend_flatten_min < 0 or self.trading.weekend_flatten_min > 1440:
            errors.append("trading.weekend_flatten_min must be >= 0 and <= 1440")
        if self.trading.week_open_grace_min < 0 or self.trading.week_open_grace_min > 1440:
            errors.append("trading.week_open_grace_min must be >= 0 and <= 1440")
        if not 1 <= self.trading.max_positions_per_symbol <= MAX_POSITIONS_PER_SYMBOL_LIMIT:
            errors.append(
                f"trading.max_positions_per_symbol must be >= 1 and <= {MAX_POSITIONS_PER_SYMBOL_LIMIT}"
            )

        if not self.enabled_personas:
            errors.append("enabled_personas must be non-empty")

        return errors

    def to_dataclass(self) -> dict:
        """Convert Config into a plain dict with typed values and stripped secrets."""
        import copy
        safe = copy.deepcopy(self)
        # Strip secrets
        safe.exchange.api_key = "***" if safe.exchange.api_key else ""
        safe.exchange.api_secret = "***" if safe.exchange.api_secret else ""
        safe.exchange.password = "***" if safe.exchange.password else ""
        safe.telegram.bot_token = "***" if safe.telegram.bot_token else ""
        safe.llm.api_key = "***" if safe.llm.api_key else ""
        safe.systemone_api_key = "***" if safe.systemone_api_key else ""

        # Flatten trading fields for direct consumption
        d = copy.deepcopy(safe.trading.__dict__)
        d["exchange"] = copy.deepcopy(safe.exchange.__dict__)
        d["telegram"] = copy.deepcopy(safe.telegram.__dict__)
        d["llm"] = copy.deepcopy(safe.llm.__dict__)
        d["database"] = copy.deepcopy(safe.database.__dict__)
        d["build"] = copy.deepcopy(safe.build.__dict__)
        d["headless"] = safe.headless
        d["paper_trading"] = safe.paper_trading
        d["debug"] = safe.debug
        d["log_level"] = safe.log_level
        d["enabled_personas"] = list(safe.enabled_personas)
        d["systemone_enabled"] = safe.systemone_enabled
        d["systemone_endpoint"] = safe.systemone_endpoint
        d["systemone_api_key"] = safe.systemone_api_key
        return d

    def to_json(self) -> str:
        """Serialize config to JSON (excluding sensitive keys)"""
        import copy
        safe = copy.deepcopy(self)
        safe.exchange.api_key = "***" if safe.exchange.api_key else ""
        safe.exchange.api_secret = "***" if safe.exchange.api_secret else ""
        safe.exchange.password = "***" if safe.exchange.password else ""
        safe.telegram.bot_token = "***" if safe.telegram.bot_token else ""
        safe.llm.api_key = "***" if safe.llm.api_key else ""
        safe.systemone_api_key = "***" if safe.systemone_api_key else ""
        return json.dumps(safe.__dict__, indent=2, default=str)

    def save(self, path: str) -> None:
        """Save config to JSON file"""
        with open(path, 'w') as f:
            f.write(self.to_json())


_config: Optional[Config] = None


def get_config() -> Config:
    """Get global config instance (singleton)"""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def set_config(config: Config) -> None:
    """Set global config instance"""
    global _config
    _config = config
