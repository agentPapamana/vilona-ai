#!/usr/bin/env python3
"""MT5 Command Relay Agent.
Acts as a local bridge between the MT5 MQL5 EA and Vilona AI / MCP.
Runs on localhost/0.0.0.0:8787 on the host machine running MetaTrader 5.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, os, secrets, sys, threading, time

HOST = os.environ.get("MT5_AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("MT5_AGENT_PORT", "8787"))
TOKEN = os.environ.get("MT5_AGENT_TOKEN", "TzYS794bznUq8wLqBn8Tkp7WJJ3yPbPEpXQbarzZUNE")

lock = threading.Lock()
commands = {}
last_ea_poll = 0.0

# Commands the EA can answer without changing terminal state. Read tools are
# always allowed; order-mutating commands are listed separately because the
# MCP server is the layer that authorizes them.
READ_ONLY = {"ping", "account_info", "get_tick", "get_positions", "get_candles",
             "get_candles_range", "get_symbol_info", "get_pending_orders",
             "calculate_daily_pl", "symbol_limits", "history_deals",
             "get_server_day_start", "get_history_deals"}
MUTATING = {"send_market_order", "modify_position_sltp", "close_position"}
ALLOWED = READ_ONLY | MUTATING

class Handler(BaseHTTPRequestHandler):
    server_version = "mt5-agent/1.0"
    protocol_version = "HTTP/1.1"

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
        if not self._auth():
            return self._send(401, {"success": False, "error": "unauthorized"})

        if self.path == "/health":
            now = time.time()
            ea_connected = (now - last_ea_poll) < 10.0 if last_ea_poll > 0 else False
            return self._send(200, {
                "success": True,
                "agent": True,
                "ea": ea_connected,
                "last_ea_poll_seconds_ago": round(now - last_ea_poll, 1) if last_ea_poll > 0 else None,
            })

        if self.path == "/poll":
            now = time.time()
            with lock:
                last_ea_poll = now
                pending = next((dict(v, id=k) for k, v in commands.items() if v["status"] == "queued"), None)
                if pending:
                    commands[pending["id"]]["status"] = "claimed"
            return self._send(200, pending or {"success": True, "command": None})

        if self.path.startswith("/command/"):
            cid = self.path.rsplit("/", 1)[-1]
            with lock:
                item = commands.get(cid)
            if not item:
                return self._send(404, {"success": False, "error": "command_not_found"})
            return self._send(200, item)

        return self._send(404, {"success": False, "error": "not_found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"success": False, "error": "unauthorized"})

        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"success": False, "error": f"invalid_json: {e}"})

        if self.path == "/command":
            name = body.get("command")
            if name not in ALLOWED:
                return self._send(403, {"success": False, "error": f"command_not_allowed: {name}"})
            cid = secrets.token_hex(12)
            with lock:
                commands[cid] = {
                    "status": "queued",
                    "command": name,
                    "args": body.get("args", {}),
                    "created": time.time(),
                }
            return self._send(202, {"success": True, "id": cid})

        if self.path.startswith("/result/"):
            cid = self.path.rsplit("/", 1)[-1]
            with lock:
                if cid not in commands:
                    return self._send(404, {"success": False, "error": "command_not_found"})
                commands[cid].update({"status": "done", "result": body})
            return self._send(200, {"success": True})

        return self._send(404, {"success": False, "error": "not_found"})

    def log_message(self, format: str, *args):
        # Concise logging for production
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {format % args}\n")

if __name__ == "__main__":
    print(f"MT5 Relay Agent listening on {HOST}:{PORT}", flush=True)
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
