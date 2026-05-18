#!/bin/bash
# Brain Memory System — Session Save Hook (Stop event)
# Ships a transcript excerpt + structured signals to the Brain server.
# The thalamus does the synthesis (summary + key_decisions) server-side;
# this hook stays a thin transport so SaaS users get identical extraction quality.

set -uo pipefail

HOOK_INPUT=$(cat)

NAMESPACE="${BRAIN_NAMESPACE:-$(basename "$PWD")}"
SESSION_ID="${BRAIN_SESSION_ID:-unknown}"
TRANSCRIPT_PATH=$(echo "$HOOK_INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")

BRAIN_URL="${BRAIN_URL:-http://localhost:8002}"
BRAIN_MCP_URL="${BRAIN_URL%/}/mcp/"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/local/brain}"
BRAIN_TOKEN_SCRIPT="${PLUGIN_ROOT}/bin/get-brain-token.sh"
HEADERS_TMP=$(mktemp)

if [ ! -x "$BRAIN_TOKEN_SCRIPT" ]; then
  rm -f "$HEADERS_TMP"
  exit 0
fi

TOKEN_JSON=$("$BRAIN_TOKEN_SCRIPT" 2>/dev/null || echo '{}')
AUTH=$(echo "$TOKEN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('Authorization',''))" 2>/dev/null || echo "")

if [ -z "$AUTH" ]; then
  rm -f "$HEADERS_TMP"
  exit 0
fi

PAYLOAD_JSON='{}'
if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  PAYLOAD_JSON=$(TRANSCRIPT_PATH="$TRANSCRIPT_PATH" python3 <<'PYEOF' 2>/dev/null || echo '{}'
import json, os

transcript = os.environ.get("TRANSCRIPT_PATH", "")
lines = []
try:
    with open(transcript) as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
except OSError:
    pass

assistant_texts = []
files_modified = []
tools_used = []
first_user_message = None

for entry in lines:
    msg = entry.get("message", {})
    role = msg.get("role", entry.get("role", ""))
    content = msg.get("content", [])
    if isinstance(content, str):
        if role == "user" and first_user_message is None:
            first_user_message = content
        continue
    for block in content:
        btype = block.get("type", "")
        if role == "user" and btype == "text" and first_user_message is None:
            first_user_message = block.get("text", "") or None
        if role == "assistant":
            if btype == "text":
                t = block.get("text", "")
                if t and len(t) > 20:
                    assistant_texts.append(t)
            elif btype == "tool_use":
                name = block.get("name", "")
                if name and name not in tools_used:
                    tools_used.append(name)
                inp = block.get("input", {}) or {}
                fp = inp.get("file_path", "")
                if fp and name in ("Edit", "Write", "NotebookEdit") and fp not in files_modified:
                    files_modified.append(fp)

excerpt = "\n\n".join(assistant_texts[-12:])
if len(excerpt) > 16000:
    excerpt = excerpt[-16000:]

print(json.dumps({
    "transcript_excerpt": excerpt,
    "first_user_message": (first_user_message or "")[:500],
    "files_modified": files_modified[:20],
    "tools_used": tools_used[:15],
}))
PYEOF
)
fi

INIT=$(curl -s --max-time 8 -D "$HEADERS_TMP" \
  -H "Authorization: $AUTH" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$BRAIN_MCP_URL" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"brain-hook-stop","version":"1.0"}},"id":0}' 2>/dev/null)

MCP_SID=$(grep -i 'mcp-session-id' "$HEADERS_TMP" 2>/dev/null | tr -d '\r' | awk '{print $2}')
rm -f "$HEADERS_TMP"

if [ -z "$MCP_SID" ]; then
  exit 0
fi

curl -s --max-time 3 \
  -H "Authorization: $AUTH" \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $MCP_SID" \
  -X POST "$BRAIN_MCP_URL" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null 2>&1

SAVE_BODY=$(NAMESPACE="$NAMESPACE" SESSION_ID="$SESSION_ID" PAYLOAD_JSON="$PAYLOAD_JSON" python3 <<'PYEOF' 2>/dev/null
import json, os
payload = {}
try:
    payload = json.loads(os.environ.get("PAYLOAD_JSON", "{}"))
except json.JSONDecodeError:
    payload = {}
args = {
    "namespace": os.environ["NAMESPACE"],
    "action": "save",
    "session_id": os.environ["SESSION_ID"],
    "outcome": "completed",
}
if payload.get("transcript_excerpt"):
    args["transcript_excerpt"] = payload["transcript_excerpt"]
if payload.get("first_user_message"):
    args["first_user_message"] = payload["first_user_message"]
if payload.get("files_modified"):
    args["files_modified"] = payload["files_modified"]
if payload.get("tools_used"):
    args["tools_used"] = payload["tools_used"]
print(json.dumps({
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {"name": "ss", "arguments": args},
    "id": 1,
}))
PYEOF
)

curl -s --max-time 15 \
  -H "Authorization: $AUTH" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $MCP_SID" \
  -X POST "$BRAIN_MCP_URL" \
  -d "$SAVE_BODY" > /dev/null 2>&1

exit 0
