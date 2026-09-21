#!/usr/bin/env bash
set -euo pipefail

TOOLS_URL="${TOOLS_URL:-http://localhost:9102/mcp}"
HEADERS_FILE="${HEADERS_FILE:-/ai/server/tools/mcp_headers.txt}"

# 1) Initialize a session
curl -s -D "$HEADERS_FILE" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$TOOLS_URL" \
  -d '{
    "jsonrpc":"2.0",
    "id":1,
    "method":"initialize",
    "params":{
      "protocolVersion":"1.1",
      "capabilities":{"experimental":null},
      "clientInfo":{"name":"curl-test","version":"0.1.0"}
    }
  }'

# Extract the mcp-session-id from the response headers
SESSION_ID=$(grep -i "mcp-session-id" "$HEADERS_FILE" | awk '{print $2}' | tr -d "\r")

# 2) Call the Home Assistant tool using the same session id
curl -N \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: ${SESSION_ID}" \
  -X POST "$TOOLS_URL" \
  -d '{
    "jsonrpc":"2.0",
    "id":2,
    "method":"tools/call",
    "params":{
      "name":"homeassistant__HassTurnOn",
      "arguments":{
        "name":"living room lights",
        "device_classes":["light"]
      }
    }
  }'
