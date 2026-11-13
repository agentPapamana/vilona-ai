#!/usr/bin/env bash
# Run Vilona AI app_server.py against the live MT5 demo bridge.
# MCP server (Fedora :8765) -> Mac agent (:8787) -> MQL5 EA -> MT5 terminal.
set -euo pipefail

cd /mnt/hermes-workspace/Apps/vilona-ai

# .env in this repo uses the `vilona_` prefix; nothing loads it automatically.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

export KINGAI_EXCHANGE=mt5
export KINGAI_MT5_TRANSPORT=mcp
export KINGAI_MT5_MCP_URL=http://127.0.0.1:8765
export KINGAI_HEADLESS=true
export KINGAI_PAPER_TRADING="${KINGAI_PAPER_TRADING:-true}"
export KINGAI_LOG_LEVEL=INFO

echo "[vilona] starting app_server.py at $(date '+%F %T')"
# Print the effective mode and the bridge/account identity before any order can
# be emitted, so the operator's screen proves which mode is actually running.
if [ "${KINGAI_PAPER_TRADING}" = "false" ]; then
    echo "[vilona] MODE: LIVE-DEMO (KINGAI_PAPER_TRADING=false) -- real broker orders"
else
    echo "[vilona] MODE: PAPER (KINGAI_PAPER_TRADING=${KINGAI_PAPER_TRADING})"
fi
echo "[vilona] account=${KINGAI_MT5_ACCOUNT:-unset} bridge=${KINGAI_MT5_MCP_URL} transport=${KINGAI_MT5_TRANSPORT}"
echo "[vilona] exchange=mt5 transport=mcp mcp=${KINGAI_MT5_MCP_URL} symbols=${KINGAI_SYMBOLS}"

# Single-instance guard. Two app_server processes share one SQLite file and one
# MT5 bridge: they duplicate scan work, fight over the DB write lock, and can
# each emit an order for the same signal. A restart whose stop signal did not
# land must abort loudly instead of silently doubling the scanner.
LOCK_FILE="${vilona_LOCK_FILE:-/tmp/vilona-app-server.lock}"
# Record the pid in a sidecar so a refused start can name the holder. The lock
# file itself is truncated by the redirect below, so it cannot carry the pid.
PID_FILE="${LOCK_FILE}.pid"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    holder=$(cat "${PID_FILE}" 2>/dev/null || true)
    [ -n "${holder}" ] || holder=$(pgrep -f 'python.*app_server\.py' | tr '\n' ' ')
    echo "[vilona] REFUSING TO START: another app_server.py holds ${LOCK_FILE} (pid ${holder:-unknown})" >&2
    echo "[vilona] stop the running instance first: pkill -f 'app_server.py'" >&2
    exit 1
fi
printf '%s\n' "$$" > "${PID_FILE}" 2>/dev/null || true
echo "[vilona] single-instance lock acquired: ${LOCK_FILE} (pid $$)"

exec /mnt/hermes-workspace/Apps/vilona-ai/.venv/bin/python -u app_server.py
