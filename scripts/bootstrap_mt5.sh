#!/usr/bin/env bash
# ==============================================================================
# VILONA AI — MT5 BRIDGE BOOTSTRAPPER (v1.2.0)
# ==============================================================================
# One-click bootstrap script for target MT5 environments (macOS Wine, Linux, Win).
# Automatically installs EA, configures WebRequest & AutoTrading, compiles EA,
# sets up headless auto-run configuration, and starts the local relay agent.
# ==============================================================================
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_TOKEN="TzYS794bznUq8wLqBn8Tkp7WJJ3yPbPEpXQbarzZUNE"
AGENT_TOKEN="${MT5_AGENT_TOKEN:-$DEFAULT_TOKEN}"
AGENT_PORT="${MT5_AGENT_PORT:-8787}"
DATA_DIR_ARG=""

print_banner() {
    echo -e "${CYAN}${BOLD}"
    echo "==================================================================="
    echo "      VILONA AI — MT5 BRIDGE & HEADLESS EA BOOTSTRAPPER            "
    echo "==================================================================="
    echo -e "${NC}"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --data-dir)
                DATA_DIR_ARG="$2"
                shift 2
                ;;
            --token)
                AGENT_TOKEN="$2"
                shift 2
                ;;
            --port)
                AGENT_PORT="$2"
                shift 2
                ;;
            --help|-h)
                echo "Penggunaan: $0 [--data-dir <path>] [--token <token>] [--port <8787>]"
                exit 0
                ;;
            *)
                echo -e "${YELLOW}Unknown option: $1${NC}"
                shift
                ;;
        esac
    done
}

detect_mt5_data_dir() {
    if [ -n "$DATA_DIR_ARG" ] && [ -d "$DATA_DIR_ARG" ]; then
        echo "$DATA_DIR_ARG"
        return 0
    fi

    # 1. Cek direktori saat ini jika script ditaruh langsung di folder MT5
    if [ -d "./MQL5" ] || [ -f "./terminal64.exe" ]; then
        pwd
        return 0
    fi

    # 2. Cek path standar macOS Wine MetaTrader 5
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

    # 3. Scan dinamis di Library/Application Support untuk Wine MT5
    local dynamic_cand
    dynamic_cand=$(find "$HOME/Library/Application Support" -maxdepth 6 -type d -name "MQL5" 2>/dev/null | head -n 1 || true)
    if [ -n "$dynamic_cand" ]; then
        dirname "$dynamic_cand"
        return 0
    fi

    return 1
}

setup_relay_agent() {
    echo -e "${BOLD}[1/6] Memeriksa Relay Agent (port ${AGENT_PORT})...${NC}"

    local agent_dir="$SCRIPT_DIR/../bridge"
    mkdir -p "$agent_dir"
    local agent_script="$agent_dir/mt5_agent.py"

    # Jika script agent belum ada di bridge, buat otomatis
    if [ ! -f "$agent_script" ]; then
        cat << 'AGENT_PY' > "$agent_script"
#!/usr/bin/env python3
import json, os, secrets, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("MT5_AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("MT5_AGENT_PORT", "8787"))
TOKEN = os.environ.get("MT5_AGENT_TOKEN", "TzYS794bznUq8wLqBn8Tkp7WJJ3yPbPEpXQbarzZUNE")

lock = threading.Lock()
commands = {}
last_ea_poll = 0.0
READ_ONLY = {"ping", "account_info", "get_tick", "get_positions", "get_candles"}

class Handler(BaseHTTPRequestHandler):
    server_version = "mt5-agent/1.0"
    def _send(self, code, obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def _auth(self):
        return self.headers.get("X-MT5-Agent-Token") == TOKEN
    def do_GET(self):
        global last_ea_poll
        if not self._auth(): return self._send(401, {"success": False, "error": "unauthorized"})
        if self.path == "/health":
            now = time.time()
            ea_connected = (now - last_ea_poll) < 10.0 if last_ea_poll > 0 else False
            return self._send(200, {
                "success": True, "agent": True, "ea": ea_connected,
                "last_ea_poll_seconds_ago": round(now - last_ea_poll, 1) if last_ea_poll > 0 else None,
            })
        if self.path == "/poll":
            now = time.time()
            with lock:
                last_ea_poll = now
                pending = next((dict(v, id=k) for k, v in commands.items() if v["status"] == "queued"), None)
                if pending: commands[pending["id"]]["status"] = "claimed"
            return self._send(200, pending or {"success": True, "command": None})
        if self.path.startswith("/command/"):
            cid = self.path.rsplit("/", 1)[-1]
            with lock: item = commands.get(cid)
            if not item: return self._send(404, {"success": False, "error": "command_not_found"})
            return self._send(200, item)
        return self._send(404, {"success": False, "error": "not_found"})
    def do_POST(self):
        if not self._auth(): return self._send(401, {"success": False, "error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e: return self._send(400, {"success": False, "error": f"invalid_json: {e}"})
        if self.path == "/command":
            name = body.get("command")
            if name not in READ_ONLY: return self._send(403, {"success": False, "error": "only_read_only_commands_enabled"})
            cid = secrets.token_hex(12)
            with lock: commands[cid] = {"status": "queued", "command": name, "args": body.get("args", {}), "created": time.time()}
            return self._send(202, {"success": True, "id": cid})
        if self.path.startswith("/result/"):
            cid = self.path.rsplit("/", 1)[-1]
            with lock:
                if cid not in commands: return self._send(404, {"success": False, "error": "command_not_found"})
                commands[cid].update({"status": "done", "result": body})
            return self._send(200, {"success": True})
        return self._send(404, {"success": False, "error": "not_found"})
    def log_message(self, format: str, *args):
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {format % args}\n")

if __name__ == "__main__":
    print(f"MT5 Relay Agent listening on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
AGENT_PY
        chmod +x "$agent_script"
    fi

    # Cek apakah port 8787 sudah aktif
    local port_busy=0
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$AGENT_PORT" -sTCP:LISTEN >/dev/null 2>&1 && port_busy=1
    elif command -v ss >/dev/null 2>&1; then
        ss -lntp 2>/dev/null | grep -q ":$AGENT_PORT " && port_busy=1
    fi

    if [ "$port_busy" -eq 1 ]; then
        echo -e "  ${GREEN}✓ Port ${AGENT_PORT} sudah listen.${NC}"
    else
        echo -e "  ${YELLOW}→ Menjalankan agent di background...${NC}"
        MT5_AGENT_TOKEN="$AGENT_TOKEN" nohup python3 "$agent_script" > "$agent_dir/agent.log" 2>&1 &
        local pid=$!
        echo "$pid" > "$agent_dir/agent.pid"
        sleep 1
        echo -e "  ${GREEN}✓ Agent berjalan (PID $pid, log: $agent_dir/agent.log)${NC}"
    fi
}

deploy_ea() {
    local target_dir="$1"
    echo -e "${BOLD}[2/6] Memasang MT5BridgeEA ke folder MQL5...${NC}"

    local ea_dir="$target_dir/MQL5/Experts/Vilona"
    local preset_dir="$target_dir/MQL5/Presets"
    mkdir -p "$ea_dir" "$preset_dir"

    local src_mq5="$SCRIPT_DIR/../mql5/Experts/Vilona/MT5BridgeEA.mq5"
    local dest_mq5="$ea_dir/MT5BridgeEA.mq5"

    if [ -f "$src_mq5" ]; then
        cp -f "$src_mq5" "$dest_mq5"
    else
        echo -e "  ${YELLOW}→ Membuat MT5BridgeEA.mq5 di target...${NC}"
        # Salin EA source dengan token terkonfigurasi
        sed "s/input string AgentToken = .*/input string AgentToken = \"$AGENT_TOKEN\";/" \
            "$SCRIPT_DIR/../mql5/Experts/Vilona/MT5BridgeEA.mq5" 2>/dev/null > "$dest_mq5" || true
    fi

    # Buat preset .set untuk auto-load parameter
    cat << EOF > "$preset_dir/vilona.set"
AgentURL=http://127.0.0.1:8787
AgentToken=$AGENT_TOKEN
PollMs=500
Verbose=true
EOF
    echo -e "  ${GREEN}✓ EA tersalin ke: $dest_mq5${NC}"
    echo -e "  ${GREEN}✓ Preset tersimpan: $preset_dir/vilona.set${NC}"
}

compile_ea_if_possible() {
    local target_dir="$1"
    echo -e "${BOLD}[3/6] Memeriksa Kompiler MetaEditor...${NC}"

    local metaeditor=""
    local candidates=(
        "$target_dir/metaeditor64.exe"
        "$target_dir/metaeditor.exe"
        "$(dirname "$target_dir")/metaeditor64.exe"
    )

    for cand in "${candidates[@]}"; do
        if [ -f "$cand" ]; then
            metaeditor="$cand"
            break
        fi
    done

    if [ -n "$metaeditor" ]; then
        echo -e "  ${CYAN}Ditemukan: $metaeditor${NC}"
        local mq5_file="$target_dir/MQL5/Experts/Vilona/MT5BridgeEA.mq5"
        local log_file="$target_dir/MQL5/Experts/Vilona/compile.log"

        if command -v wine >/dev/null 2>&1; then
            echo -e "  ${YELLOW}Mengompilasi headless via Wine...${NC}"
            wine "$metaeditor" "/compile:$mq5_file" "/log:$log_file" || true
        elif [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* || "$OSTYPE" == "win32"* ]]; then
            "$metaeditor" "/compile:$mq5_file" "/log:$log_file" || true
        fi

        if [ -f "$target_dir/MQL5/Experts/Vilona/MT5BridgeEA.ex5" ]; then
            echo -e "  ${GREEN}✓ Sukses kompilasi: MT5BridgeEA.ex5 berhasil dibuat!${NC}"
        else
            echo -e "  ${YELLOW}Kompilasi otomatis belum menghasilkan .ex5. Silakan tekan F7 di MetaEditor.${NC}"
        fi
    else
        echo -e "  ${YELLOW}MetaEditor CLI tidak ditemukan di path MT5. Silakan compile di MetaEditor (F7).${NC}"
    fi
}

configure_mt5_inifiles() {
    local target_dir="$1"
    echo -e "${BOLD}[4/6] Mengonfigurasi WebRequest & AutoTrading MT5...${NC}"

    local config_dir="$target_dir/config"
    mkdir -p "$config_dir"

    # Injeksi ke common.ini & terminal.ini
    for ini in "$config_dir/common.ini" "$config_dir/terminal.ini"; do
        touch "$ini"
        if ! grep -q "\[Experts\]" "$ini" 2>/dev/null; then
            cat << 'EOF' >> "$ini"

[Experts]
AllowDllImport=1
Enabled=1
Account=1
Profile=1
WebRequest=1
WebRequestUrl=http://127.0.0.1:8787,http://100.95.94.124:8787
EOF
        else
            # Pastikan WebRequest dan URL aktif
            grep -q "WebRequest=1" "$ini" || echo "WebRequest=1" >> "$ini"
            grep -q "WebRequestUrl=" "$ini" || echo "WebRequestUrl=http://127.0.0.1:8787,http://100.95.94.124:8787" >> "$ini"
            grep -q "Enabled=1" "$ini" || echo "Enabled=1" >> "$ini"
        fi
    done
    echo -e "  ${GREEN}✓ Izin WebRequest & AlgoTrading diinjeksi ke config MT5.${NC}"
}

setup_headless_autorun() {
    local target_dir="$1"
    echo -e "${BOLD}[5/6] Menyiapkan Konfigurasi Startup Headless / Auto-Run...${NC}"

    local config_dir="$target_dir/config"
    local startup_ini="$config_dir/startup_vilona.ini"

    # File konfigurasi startup MT5
    cat << EOF > "$startup_ini"
[Common]
EnableAPI=true
EnableDDE=true

[Experts]
AllowDllImport=1
Enabled=1
Account=1
Profile=1

[Start]
Expert=Vilona\\MT5BridgeEA
ExpertParameters=vilona.set
Symbol=EURUSD
Period=H1
EOF

    # Buat launcher script untuk headless / one-click run
    local launcher="$target_dir/run_mt5_headless.sh"
    cat << 'EOF' > "$launcher"
#!/usr/bin/env bash
# ==============================================================================
# Launcher One-Click MetaTrader 5 + Vilona Bridge
# ==============================================================================
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$DIR/config/startup_vilona.ini"

echo "[Vilona MT5] Memulai MetaTrader 5 dengan auto-attached Bridge EA..."

if [ -f "$DIR/terminal64.exe" ]; then
    if command -v wine >/dev/null 2>&1; then
        exec wine "$DIR/terminal64.exe" "/config:$CONFIG" "/portable"
    else
        exec "$DIR/terminal64.exe" "/config:$CONFIG" "/portable"
    fi
elif [ -d "/Applications/MetaTrader 5.app" ]; then
    open -a "MetaTrader 5" --args "/config:$CONFIG"
else
    echo "Terminal MT5 executable tidak ditemukan di $DIR."
    echo "Buka MT5 manual lalu load EA 'Vilona/MT5BridgeEA' pada chart."
fi
EOF
    chmod +x "$launcher"
    echo -e "  ${GREEN}✓ Startup INI dibuat: $startup_ini${NC}"
    echo -e "  ${GREEN}✓ Script launcher dibuat: $launcher${NC}"
}

verify_connection() {
    echo -e "${BOLD}[6/6] Verifikasi Konektivitas Loopback...${NC}"

    if command -v curl >/dev/null 2>&1; then
        local health_resp
        health_resp=$(curl -s -m 3 -H "X-MT5-Agent-Token: $AGENT_TOKEN" "http://127.0.0.1:$AGENT_PORT/health" || echo "")
        if [ -n "$health_resp" ]; then
            echo -e "  ${GREEN}✓ Endpoint agent merespons: $health_resp${NC}"
        else
            echo -e "  ${YELLOW}⚠ Agent belum merespons curl. Cek log di bridge/agent.log.${NC}"
        fi
    fi
}

main() {
    print_banner
    parse_args "$@"

    local mt5_dir=""
    if mt5_dir=$(detect_mt5_data_dir); then
        echo -e "${GREEN}${BOLD}✓ MetaTrader 5 Data Directory terdeteksi:${NC} $mt5_dir"
    else
        echo -e "${YELLOW}Data directory MT5 tidak terdeteksi otomatis.${NC}"
        echo -n "Masukkan path folder Data MT5 (atau tekan Enter untuk path saat ini): "
        read -r user_path
        mt5_dir="${user_path:-$(pwd)}"
    fi

    setup_relay_agent
    deploy_ea "$mt5_dir"
    compile_ea_if_possible "$mt5_dir"
    configure_mt5_inifiles "$mt5_dir"
    setup_headless_autorun "$mt5_dir"
    verify_connection

    echo -e "${GREEN}${BOLD}"
    echo "==================================================================="
    echo "                BOOTSTRAP MT5 BRIDGE SELESAI!                     "
    echo "==================================================================="
    echo -e "${NC}"
    echo -e "Pilihan cara menjalankan:"
    echo -e "  1. ${BOLD}Headless / Auto-Run (Rekomendasi):${NC}"
    echo -e "     Jalankan script: ${CYAN}$mt5_dir/run_mt5_headless.sh${NC}"
    echo -e "  2. ${BOLD}Manual GUI:${NC}"
    echo -e "     Buka MT5 ➔ Navigator ➔ Refresh ➔ Drag 'MT5BridgeEA' ke chart."
    echo -e "     (Parameter URL & Token sudah otomatis terisi default, tinggal klik OK)."
    echo ""
}

main "$@"
