#!/usr/bin/env python3

from trading.bridge_mt5_mcp import MT5MCPBridge

# Test the MCP bridge in isolation
bridge = MT5MCPBridge(mcp_url="http://127.0.0.1:8765", timeout=10)
print("Testing MCP handshake...")

try:
    connected = bridge.connect()
    print(f"connect() returned: {connected}")
    print(f"Session ID: {bridge._session_id}")
    
    if connected:
        print("✅ Bridge reports connected")
    else:
        print("❌ Bridge reports disconnected (expected for stub)")
        
except Exception as e:
    print(f"💥 Connection failed with exception: {e}")

print("Test complete.")
