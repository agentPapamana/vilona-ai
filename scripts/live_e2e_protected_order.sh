#!/usr/bin/env bash
# Live E2E: one real XAUUSD order through the protected-order gate.
# Proves: SL is sent, SL is still on the position after fill, position is
# closed afterwards, account left flat. Any safety rule violation aborts.
set -u

cd /mnt/hermes-workspace/Apps/vilona-ai || exit 1
set -a
# shellcheck disable=SC1091
. ./.env
set +a

echo "=============================================================="
echo " VILONA LIVE E2E - protected order on demo account"
echo " commit : $(git rev-parse --short HEAD)"
echo " branch : $(git rev-parse --abbrev-ref HEAD)"
echo " python : $(/tmp/vilvenv/bin/python -V 2>&1)"
echo " time   : $(date -Is)"
echo "=============================================================="

PYTHONPATH=. /tmp/vilvenv/bin/python -u - <<'PY'
import time
from trading.bridge_mt5_mcp import MT5MCPBridge
from core.order_safety import place_protected_order, resolve_magic

SYMBOL = "XAUUSD"
STEP = 0.01
bridge = MT5MCPBridge(mcp_url="http://127.0.0.1:8765", timeout=30)
bridge.connect()

acct = bridge.get_account_info()
print(f"[1] account  login={acct.login} server={acct.server} balance={acct.balance:.2f}")
if "demo" not in str(acct.server).lower():
    raise SystemExit("ABORT: server is not a demo account")

existing = bridge.get_positions() or []
print(f"[2] book     open_positions={len(existing)}")
if existing:
    raise SystemExit("ABORT: account not flat; refusing to place a test order")

tick = bridge.get_tick(SYMBOL)
info = bridge.get_symbol_info(SYMBOL)
ask = float(tick.ask)
digits = int(info.digits)
stops = float(getattr(info, "stops_level", 0) or 0) * float(info.point)
from core.order_safety import resolve_min_stop_distance
from config import get_config

min_dist = resolve_min_stop_distance(SYMBOL, get_config().trading)
sl = round(ask - min_dist, digits)
tp = round(ask + min_dist * 3, digits)
magic = resolve_magic(SYMBOL)
print(f"[3] params   {SYMBOL} BUY ask={ask} sl={sl} tp={tp} magic={magic} digits={digits}")

res = place_protected_order(
    bridge, symbol=SYMBOL, action="buy", volume=STEP,
    sl=sl, tp=tp, magic=magic, comment="vilona-auto",
    reference_price=ask, min_stop_distance=min_dist, digits=digits,
)
print(f"[4] gate     success={res.success} sl_verified={res.sl_verified} "
      f"ticket={res.ticket} msg={res.message}")
if not res.success:
    raise SystemExit("FAIL: protected order rejected")

time.sleep(2)
pos = next((p for p in (bridge.get_positions(SYMBOL) or []) if p.ticket == res.ticket), None)
if pos is None:
    raise SystemExit(f"FAIL: ticket {res.ticket} not present after fill")
print(f"[5] broker   ticket={pos.ticket} sl={pos.sl} tp={pos.tp} magic={pos.magic} "
      f"vol={pos.volume} pnl={pos.profit}")
if pos.sl <= 0:
    raise SystemExit("FAIL: position is naked (sl=0) after fill")
if pos.magic != magic:
    raise SystemExit(f"FAIL: magic mismatch {pos.magic} != {magic}")
if abs(pos.sl - sl) > 10 ** (-digits):
    raise SystemExit(f"FAIL: SL drifted {pos.sl} != {sl}")
print("[6] VERIFIED stop-loss is on the position and owned by magic")

close = bridge.close_position(ticket=pos.ticket, symbol=SYMBOL,
                              volume=pos.volume, comment="e2e cleanup", magic=magic)
print(f"[7] cleanup  close success={close.success} msg={close.message}")

time.sleep(2)
final = bridge.get_positions() or []
acct2 = bridge.get_account_info()
print(f"[8] flat     open_positions={len(final)} balance={acct2.balance:.2f}")
if final:
    raise SystemExit(f"FAIL: positions still open: {[p.ticket for p in final]}")
print("[9] E2E PASS")
PY
