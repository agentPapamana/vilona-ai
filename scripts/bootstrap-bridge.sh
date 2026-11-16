#!/usr/bin/env bash
# Synchronize MT5 bridge source and EA from this repository to the Mac.
# Linux: MAC_HOST=juandisay@shit-mac MAC_REPO=/Users/juandisay/apps/vilona-ai MT5_DATA_DIR=".../MQL5" scripts/bootstrap-bridge.sh deploy
# Mac: SOURCE_URL=git@github.com:agentPapamana/vilona-ai.git MAC_REPO="$HOME/apps/vilona-ai" MT5_DATA_DIR=".../MQL5" scripts/bootstrap-bridge.sh bootstrap
# Never compiles or attaches the EA and never starts trading.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd 2>/dev/null || pwd)"
REMOTE_REPO="${MAC_REPO:-${REMOTE_REPO:-$HOME/vilona-ai}}"
MAC_HOST="${MAC_HOST:-}"
SOURCE_URL="${SOURCE_URL:-git@github.com:agentPapamana/vilona-ai.git}"
SOURCE_BRANCH="${SOURCE_BRANCH:-$(git -C "$ROOT_DIR" branch --show-current 2>/dev/null || echo main)}"
SOURCE_REMOTE="${SOURCE_REMOTE:-origin}"
MT5_DATA_DIR="${MT5_DATA_DIR:-}"
AGENT_PORT="${MT5_AGENT_PORT:-8787}"
AGENT_SERVICE="${MT5_AGENT_SERVICE:-vilona-mt5-agent}"
DRY_RUN="${DRY_RUN:-false}"
log() { printf '[bootstrap-bridge] %s\n' "$*"; }
die() { printf '[bootstrap-bridge] ERROR: %s\n' "$*" >&2; exit 1; }
remote_cmd() { [ -n "$MAC_HOST" ] || die 'MAC_HOST=user@mac is required'; ssh -- "$MAC_HOST" "$@"; }
hash_file() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi; }
remote_status() {
    remote_cmd "REMOTE_REPO=$(printf '%q' "$REMOTE_REPO") MT5_DATA_DIR=$(printf '%q' "$MT5_DATA_DIR") AGENT_PORT=$(printf '%q' "$AGENT_PORT") MT5_AGENT_TOKEN=$(printf '%q' "${MT5_AGENT_TOKEN:-}") bash -s -- status" < "$ROOT_DIR/scripts/bootstrap-bridge.sh"
}
status_local() {
    [ -d "$REMOTE_REPO/.git" ] || die "not a git checkout: $REMOTE_REPO"
    log "repo: $REMOTE_REPO"; git -C "$REMOTE_REPO" status --short -- bridge mql5; git -C "$REMOTE_REPO" log -1 --format='commit %h %ci %s'
    [ -f "$REMOTE_REPO/bridge/mt5_agent.py" ] && hash_file "$REMOTE_REPO/bridge/mt5_agent.py" || log 'bridge source missing'
    [ -f "$REMOTE_REPO/mql5/Experts/Vilona/MT5BridgeEA.mq5" ] && hash_file "$REMOTE_REPO/mql5/Experts/Vilona/MT5BridgeEA.mq5" || log 'EA source missing'
    [ -n "$MT5_DATA_DIR" ] && [ -f "$MT5_DATA_DIR/Experts/Vilona/MT5BridgeEA.mq5" ] && hash_file "$MT5_DATA_DIR/Experts/Vilona/MT5BridgeEA.mq5" || true
    curl --fail --silent --show-error -H "X-MT5-Agent-Token: ${MT5_AGENT_TOKEN:-}" "http://127.0.0.1:${AGENT_PORT}/health" || log 'agent health unavailable'
}
bootstrap_local() {
    command -v git >/dev/null || die 'git is required'; mkdir -p "$(dirname "$REMOTE_REPO")"
    if [ ! -d "$REMOTE_REPO/.git" ]; then git clone --branch "$SOURCE_BRANCH" "$SOURCE_URL" "$REMOTE_REPO"; else
        [ "$DRY_RUN" = true ] && log "would fetch $SOURCE_REMOTE and reset $SOURCE_BRANCH" || { git -C "$REMOTE_REPO" fetch --prune "$SOURCE_REMOTE"; git -C "$REMOTE_REPO" checkout "$SOURCE_BRANCH"; git -C "$REMOTE_REPO" reset --hard "$SOURCE_REMOTE/$SOURCE_BRANCH"; }
    fi
    if [ -n "$MT5_DATA_DIR" ]; then mkdir -p "$MT5_DATA_DIR/Experts/Vilona"; cp "$REMOTE_REPO/mql5/Experts/Vilona/MT5BridgeEA.mq5" "$MT5_DATA_DIR/Experts/Vilona/MT5BridgeEA.mq5"; log 'EA source copied; compile and attach it in MetaEditor'; fi
    if command -v launchctl >/dev/null && [ -f "$HOME/Library/LaunchAgents/${AGENT_SERVICE}.plist" ]; then launchctl kickstart -k "gui/$(id -u)/${AGENT_SERVICE}"; fi
    status_local
}
deploy_remote() {
    [ -n "$MAC_HOST" ] || die 'MAC_HOST=user@mac is required'; [ -n "$MT5_DATA_DIR" ] || die 'MT5_DATA_DIR is required'; command -v rsync >/dev/null || die 'rsync is required'
    log "deploying bridge and EA to $MAC_HOST:$REMOTE_REPO"
    remote_cmd "mkdir -p $(printf '%q' "$REMOTE_REPO/bridge") $(printf '%q' "$REMOTE_REPO/mql5") $(printf '%q' "$MT5_DATA_DIR/Experts/Vilona")"
    if [ "$DRY_RUN" != true ]; then
        remote_cmd "mkdir -p $(printf '%q' "$REMOTE_REPO/.bootstrap-backups") && [ -f $(printf '%q' "$REMOTE_REPO/bridge/mt5_agent.py") ] && cp $(printf '%q' "$REMOTE_REPO/bridge/mt5_agent.py") $(printf '%q' "$REMOTE_REPO/.bootstrap-backups/mt5_agent.py.$(date +%Y%m%d%H%M%S)") || true"
        remote_cmd "[ -f $(printf '%q' "$MT5_DATA_DIR/Experts/Vilona/MT5BridgeEA.mq5") ] && cp $(printf '%q' "$MT5_DATA_DIR/Experts/Vilona/MT5BridgeEA.mq5") $(printf '%q' "$MT5_DATA_DIR/Experts/Vilona/MT5BridgeEA.mq5.pre-bootstrap-$(date +%Y%m%d%H%M%S)") || true"
    fi
    rsync -az --delete --exclude '__pycache__' "$ROOT_DIR/bridge/" "$MAC_HOST:$REMOTE_REPO/bridge/"
    rsync -az --delete --exclude '__pycache__' "$ROOT_DIR/mql5/" "$MAC_HOST:$REMOTE_REPO/mql5/"
    remote_cmd "cp $(printf '%q' "$REMOTE_REPO/mql5/Experts/Vilona/MT5BridgeEA.mq5") $(printf '%q' "$MT5_DATA_DIR/Experts/Vilona/MT5BridgeEA.mq5")"
    if [ "$DRY_RUN" != true ]; then remote_cmd "if command -v launchctl >/dev/null && [ -f \"$HOME/Library/LaunchAgents/${AGENT_SERVICE}.plist\" ]; then launchctl kickstart -k gui/$(id -u)/${AGENT_SERVICE}; fi"; fi
    log 'source deployed; compile/re-attach the EA in MetaEditor'; remote_status
}
case "${1:-status}" in
    bootstrap) bootstrap_local ;;
    status) [ -n "$MAC_HOST" ] && remote_status || status_local ;;
    deploy) deploy_remote ;;
    *) die "usage: $0 {bootstrap|status|deploy}" ;;
esac
