#!/usr/bin/env python3
import json, time, urllib.request

TOKEN = "TzYS794bznUq8wLqBn8Tkp7WJJ3yPbPEpXQbarzZUNE"
AGENT_URL = "http://100.95.94.124:8787"
MCP_URL = "http://127.0.0.1:8765/mcp"

def check_agent_health():
    req = urllib.request.Request(
        f"{AGENT_URL}/health",
        headers={"X-MT5-Agent-Token": TOKEN}
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def call_mcp_tool(tool_name, args={}):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    # Initialize
    r1 = urllib.request.Request(
        MCP_URL,
        data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}
        }).encode(),
        headers=h,
        method="POST"
    )
    with urllib.request.urlopen(r1, timeout=8) as resp:
        sid = resp.headers.get("mcp-session-id")
    
    # Tool call
    r2 = urllib.request.Request(
        MCP_URL,
        data=json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": args}
        }).encode(),
        headers={**h, "Mcp-Session-Id": sid},
        method="POST"
    )
    with urllib.request.urlopen(r2, timeout=12) as resp:
        res = json.loads(resp.read().decode().replace("event: message\ndata: ", "").strip())
        content_text = res.get("result", {}).get("content", [{}])[0].get("text", "{}")
        return json.loads(content_text)

if __name__ == "__main__":
    print("Checking agent health...")
    print(check_agent_health())
