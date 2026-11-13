#!/usr/bin/env bash
# Synchronize the canonical Fedora EA source to the Mac MT5 data directory.
# Compilation/reload remains a deliberate Mac-side operation in MetaEditor.
set -euo pipefail

MAC_HOST="${MT5_MAC_SSH_HOST:-100.95.94.124}"
EA_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/mql5/Experts/Vilona/MT5BridgeEA.mq5"
REMOTE_EA='/Users/juandisay/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Experts/Vilona/MT5BridgeEA.mq5'

[[ -f "$EA_SRC" ]] || { echo "missing EA source: $EA_SRC" >&2; exit 1; }
REMOTE_TMP="/tmp/MT5BridgeEA.mq5.$$"
trap 'ssh "$MAC_HOST" "rm -f $REMOTE_TMP" >/dev/null 2>&1 || true' EXIT
scp -q "$EA_SRC" "$MAC_HOST:$REMOTE_TMP"
ssh "$MAC_HOST" "cmp -s '$REMOTE_TMP' '$REMOTE_EA' || cp '$REMOTE_TMP' '$REMOTE_EA'"
LOCAL_HASH="$(md5sum "$EA_SRC" | awk '{print $1}')"
REMOTE_HASH="$(ssh "$MAC_HOST" "md5 -q '$REMOTE_EA'")"
printf 'EA sync: local_md5=%s remote_md5=%s host=%s\n' "$LOCAL_HASH" "$REMOTE_HASH" "$MAC_HOST"
[[ "$LOCAL_HASH" == "$REMOTE_HASH" ]] || { echo 'EA sync verification failed' >&2; exit 1; }
