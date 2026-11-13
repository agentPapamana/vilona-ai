from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Union, Any
from types import SimpleNamespace
from agents import Signal, MarketSnapshot
from config import TradingConfig, get_config

@dataclass
class RiskVerdict:
    approved: bool
    lot_size: float = 0.0
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    rr_ratio: float = 0.0
    reason: str = ""

def get_contract_size(symbol: str) -> float:
    """Return contract multiplier: 100,000 for standard forex, 100 for gold, 1.0 for crypto/stocks."""
    sym = (symbol or "").upper().replace("/", "").replace("_", "")
    if any(k in sym for k in ("XAU", "GOLD", "XAG", "SILVER", "OIL", "USOIL", "UKOIL")):
        return 100.0
    forex_currencies = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "SGD", "HKD"}
    clean = "".join([c for c in sym if c.isalpha()])
    if len(clean) == 6 and (clean[:3] in forex_currencies and clean[3:] in forex_currencies):
        return 100000.0
    return 1.0

def estimate_notional(symbol: str, volume: float, price: float) -> float:
    """Approximate notional exposure in account currency (USD).

    For pairs quoted in JPY, multiplying contract size by the JPY price
    inflates exposure by ~150x. Notional must be derived from the base
    currency instead: 1 lot of USDJPY is 100,000 USD.
    """
    contract_size = get_contract_size(symbol)
    if contract_size <= 1.0:
        return abs(volume) * abs(price)
    clean = "".join([c for c in (symbol or "").upper() if c.isalpha()])[:6]
    if len(clean) == 6 and clean[:3] == "USD":
        # USD/XXX: one lot is contract_size units of USD.
        return abs(volume) * contract_size
    if len(clean) == 6 and clean[3:] == "USD":
        # XXX/USD: notional in USD is volume * contract_size * price.
        return abs(volume) * contract_size * abs(price)
    # Cross or exotic: fall back to the price-based estimate.
    return abs(volume) * contract_size * abs(price)


def evaluate(
    signal: Signal,
    snapshot: MarketSnapshot,
    equity_usd: float,
    cfg: Optional[Union[TradingConfig, dict[str, Any]]] = None,
    daily_pl_usd: float = 0.0,
    open_positions_count: int = 0,
    config: Optional[dict[str, Any]] = None,
    free_margin: Optional[float] = None,
    open_positions: Optional[list] = None,
) -> RiskVerdict:
    effective_cfg = config if config is not None else cfg
    if effective_cfg is None:
        effective_cfg = get_config().trading
    elif isinstance(effective_cfg, dict):
        effective_cfg = SimpleNamespace(**effective_cfg)
    cfg = effective_cfg
    if signal.action == "WAIT":
        return RiskVerdict(False, reason="signal=WAIT")
    if signal.confidence < cfg.min_confidence:
        return RiskVerdict(False, reason=f"confidence {signal.confidence:.2f}<{cfg.min_confidence}")
    if snapshot.spread_pct > max(cfg.max_spread_pct_default, cfg.max_spread_pct):
        return RiskVerdict(False, reason=f"spread {snapshot.spread_pct:.3f}% too wide")
    if open_positions_count >= cfg.max_concurrent_positions:
        return RiskVerdict(False, reason="max concurrent positions reached")
    if daily_pl_usd <= -(equity_usd * cfg.max_daily_loss_pct / 100.0):
        return RiskVerdict(False, reason="daily loss limit hit")
    entry = signal.entry_hint or (snapshot.tick_ask if signal.action == "BUY" else snapshot.tick_bid)
    atr = snapshot.atr_15m or (entry * 0.005)
    sl_dist = cfg.sl_atr_mult * atr
    tp_dist = cfg.tp_atr_mult * atr
    if signal.action == "BUY":
        sl_price = entry - sl_dist
        tp_price = entry + tp_dist
    else:
        sl_price = entry + sl_dist
        tp_price = entry - tp_dist
    risk_usd = equity_usd * (cfg.max_risk_per_trade_pct / 100.0)
    per_unit_risk = abs(entry - sl_price)
    if per_unit_risk <= 0:
        return RiskVerdict(False, reason="zero SL distance")

    contract_size = getattr(snapshot, "contract_size", None) or get_contract_size(snapshot.symbol)

    # Preferred path: use the broker's own monetary values from symbol info.
    # tick_value = account-currency value of one tick_size move per 1.0 lot.
    # This is authoritative and handles JPY crosses, metals, and indices correctly.
    tick_value = getattr(snapshot, "tick_value", 0.0) or 0.0
    tick_size = getattr(snapshot, "tick_size", 0.0) or 0.0
    if tick_value > 0 and tick_size > 0:
        per_lot_risk = (per_unit_risk / tick_size) * tick_value
    else:
        per_lot_risk = per_unit_risk * contract_size
    if per_lot_risk <= 0:
        return RiskVerdict(False, reason="zero SL distance")

    raw_lot = risk_usd / per_lot_risk
    lot_step = float(getattr(snapshot, "lot_step", 0.0) or 0.0)
    lot_min = float(getattr(snapshot, "lot_min", 0.0) or 0.0)
    lot_max = float(getattr(snapshot, "lot_max", 0.0) or 0.0)
    # Bridge responses can omit volume fields; fall back to broker-compatible
    # defaults instead of sizing to zero and rejecting a valid signal.
    lot_step = lot_step or float(getattr(cfg, "lot_step", 0.01) or 0.01)
    lot_min = lot_min or float(getattr(cfg, "min_lot", 0.01) or 0.01)
    lot_max = lot_max or float(getattr(cfg, "max_lot", 50.0) or 50.0)
    configured_max_lot = getattr(cfg, "max_lot", lot_max) or lot_max
    lot = round(round(raw_lot / lot_step) * lot_step, 4)
    lot = min(lot, lot_max)
    minimum_volume_override = lot < lot_min
    if minimum_volume_override and lot_min > lot_max:
        return RiskVerdict(
            False,
            reason=f"broker minimum volume {lot_min:g} exceeds max lot {lot_max:g}",
        )
    if minimum_volume_override:
        # The broker minimum may be used only when its stop-loss risk is still
        # within the configured account risk budget. Never bypass risk sizing
        # merely because the account cannot trade a smaller volume.
        minimum_lot_risk = lot_min * per_lot_risk
        if minimum_lot_risk > risk_usd:
            return RiskVerdict(
                False,
                reason=(f"minimum lot risk {minimum_lot_risk:.2f} exceeds "
                        f"max risk {risk_usd:.2f}"),
            )
        lot = lot_min

    # Standard contracts use price-based exposure caps; cent-symbol broker
    # units do not map to the USD-equity notional and use stop-risk sizing.
    if not str(getattr(snapshot, "symbol", "") or "").lower().endswith("c"):
        positions = open_positions or []
        existing_notional = sum(
            estimate_notional(getattr(p, "symbol", ""), float(getattr(p, "volume", 0.0) or 0.0),
                              float(getattr(p, "price_current", 0.0) or getattr(p, "price_open", 0.0) or 0.0))
            for p in positions
        )
        per_lot_notional = estimate_notional(snapshot.symbol, 1.0, entry)
        if per_lot_notional > 0 and free_margin is not None and free_margin > 0:
            lot = min(lot, (free_margin * 0.10) / per_lot_notional)
        budget = max(0.0, equity_usd * 2.0 - existing_notional)
        if budget <= 0:
            return RiskVerdict(False, reason="notional exposure limit exceeded")
        if per_lot_notional > 0:
            lot = min(lot, budget / per_lot_notional)
        lot = round((lot // lot_step) * lot_step, 4)
    if lot <= 0:
        return RiskVerdict(False, reason="computed lot=0")


    rr = tp_dist / sl_dist if sl_dist else 0.0
    return RiskVerdict(
        approved=True,
        lot_size=lot,
        sl_price=round(sl_price, 8),
        tp_price=round(tp_price, 8),
        rr_ratio=round(rr, 2),
        reason=(f"approved conf={signal.confidence:.2f} lot={lot:.4f} rr={rr:.2f}"
                + (" broker minimum volume override" if minimum_volume_override else "")),
    )
