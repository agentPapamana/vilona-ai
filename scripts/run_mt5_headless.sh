#!/usr/bin/env bash
# ==============================================================================
# VILONA AI — MT5 HEADLESS RUNNER
# ==============================================================================
# Menjalankan MetaTrader 5 dengan auto-attach Bridge EA (Vilona\MT5BridgeEA)
# Mendukung macOS (Wine wrapper / .app), Linux Wine, dan Windows native.
# ==============================================================================
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Cari folder data MT5
detect_mt5_dir() {
    if [ -n "${MT5_DATA_DIR:-}" ] && [ -d "$MT5_DATA_DIR" ]; then
        echo "$MT5_DATA_DIR"
        return 0
    fi

    # Cek direktori saat ini
    if [ -d "./MQL5" ] || [ -f "./terminal64.exe" ]; then
        pwd
        return 0
    fi

    local candidates=(
        "$HOME/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5"
        "$HOME/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5 Terminal"
        "$HOME/Library/Application Support/com.metatrader.metatrader5/drive_c/Program Files/MetaTrader 5"
        "$HOME/.wine/drive_c/Program Files/MetaTrader 5"
        "/Applications/MetaTrader 5.app/Contents/SharedSupport/metatrader5/support/metatrader5/drive_c/Program Files/MetaTrader 5"
    )

    for cand in "${candidates[@]}"; do
        if [ -d "$cand/MQL5" ] || [ -f "$cand/terminal64.exe" ]; then
            echo "$cand"
            return 0
        fi
    done

    # Scan dinamis
    local dynamic_cand
    dynamic_cand=$(find "$HOME/Library/Application Support" -maxdepth 6 -type d -name "MQL5" 2>/dev/null | head -n 1 || true)
    if [ -n "$dynamic_cand" ]; then
        dirname "$dynamic_cand"
        return 0
    fi

    echo "$REPO_ROOT"
}

MT5_DIR="$(detect_mt5_dir)"
CONFIG_DIR="$MT5_DIR/config"
STARTUP_INI="$CONFIG_DIR/startup_vilona.ini"
PRESET_DIR="$MT5_DIR/MQL5/Presets"

echo -e "${CYAN}${BOLD}[Vilona MT5] Memulai MetaTrader 5 Headless Runner...${NC}"
echo -e "  MT5 Target Directory: ${BOLD}$MT5_DIR${NC}"

mkdir -p "$CONFIG_DIR" "$PRESET_DIR"

# Pastikan startup_vilona.ini ada
if [ ! -f "$STARTUP_INI" ]; then
    echo -e "  ${YELLOW}→ Membuat $STARTUP_INI...${NC}"
    cat << 'EOF' > "$STARTUP_INI"
[Common]
EnableAPI=true
EnableDDE=true

[Experts]
AllowDllImport=1
Enabled=1
Account=1
Profile=1

[Start]
Expert=Vilona\MT5BridgeEA
ExpertParameters=vilona.set
Symbol=EURUSD
Period=H1
EOF
fi

# Pastikan preset vilona.set ada
if [ ! -f "$PRESET_DIR/vilona.set" ]; then
    cat << 'EOF' > "$PRESET_DIR/vilona.set"
AgentURL=http://127.0.0.1:8787
AgentToken=TzYS794bznUq8wLqBn8Tkp7WJJ3yPbPEpXQbarzZUNE
PollMs=500
Verbose=true
EOF
fi

# Pastikan Relay Agent berjalan di port 8787
if ! lsof -nP -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1 && ! ss -lntp 2>/dev/null | grep -q ":8787 "; then
    echo -e "  ${YELLOW}→ Menjalankan Relay Agent di background...${NC}"
    if [ -f "$REPO_ROOT/bridge/mt5_agent.py" ]; then
        nohup python3 "$REPO_ROOT/bridge/mt5_agent.py" > "$REPO_ROOT/bridge/agent.log" 2>&1 &
        sleep 1
    fi
fi

# Eksekusi terminal MT5
if [ -f "$MT5_DIR/terminal64.exe" ]; then
    echo -e "  ${GREEN}✓ Meluncurkan terminal64.exe via startup config...${NC}"
    if command -v wine >/dev/null 2>&1; then
        exec wine "$MT5_DIR/terminal64.exe" "/config:$STARTUP_INI" "/portable"
    else
        exec "$MT5_DIR/terminal64.exe" "/config:$STARTUP_INI" "/portable"
    fi
elif [ -d "/Applications/MetaTrader 5.app" ]; then
    echo -e "  ${GREEN}✓ Membuka MetaTrader 5.app macOS dengan argumen startup config...${NC}"
    open -a "MetaTrader 5" --args "/config:$STARTUP_INI"
else
    echo -e "  ${YELLOW}File executable terminal64.exe belum ditemukan di $MT5_DIR.${NC}"
    echo -e "  ${BOLD}Cara alternatif:${NC} Buka MT5 secara manual di Mac, lalu pastikan EA 'Vilona/MT5BridgeEA' menempel pada chart."
fi
