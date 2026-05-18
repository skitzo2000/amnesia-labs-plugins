#!/bin/bash
# Brain Memory System — Session Start Hook
# 1. Sets BRAIN_NAMESPACE and BRAIN_SESSION_ID env vars (visible to all subsequent hooks)
# 2. Calls Brain MCP directly to start the session
# 3. Injects context_brief into Claude's initial context
#
# Required env (set once in your shell, e.g. ~/.bashrc):
#   BRAIN_URL              — base URL of your Brain deployment (defaults to localhost:8002)
#   KEYCLOAK_TOKEN_URL     — Keycloak token endpoint
#   KEYCLOAK_CLIENT_ID     — Brain client_id from Keycloak (e.g. brain-paul-amnesia-labs-com)
#   KEYCLOAK_CLIENT_SECRET — Brain client secret
#
# Without these the hook degrades gracefully and tells the AI to call ss() itself.

set -uo pipefail

NAMESPACE="$(basename "$PWD")"
SESSION_ID="session:$(date +%s)-${NAMESPACE}"

# Propagate env to every later hook (PreToolUse, PostToolUse, Stop)
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export BRAIN_NAMESPACE=$NAMESPACE" >> "$CLAUDE_ENV_FILE"
  echo "export BRAIN_SESSION_ID=$SESSION_ID" >> "$CLAUDE_ENV_FILE"
fi

BRAIN_URL="${BRAIN_URL:-http://localhost:8002}"
BRAIN_MCP_URL="${BRAIN_URL%/}/mcp/"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/local/brain}"
BRAIN_TOKEN_SCRIPT="${PLUGIN_ROOT}/bin/get-brain-token.sh"
HEADERS_TMP=$(mktemp)

if [ ! -x "$BRAIN_TOKEN_SCRIPT" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"Brain: token script missing. Call ss(namespace=$NAMESPACE, action=start, session_id=$SESSION_ID, project_path=$PWD, goals=infer from context) manually.\"}}"
  rm -f "$HEADERS_TMP"
  exit 0
fi

TOKEN_JSON=$("$BRAIN_TOKEN_SCRIPT" 2>/dev/null || echo '{}')
AUTH=$(echo "$TOKEN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('Authorization',''))" 2>/dev/null || echo "")

if [ -z "$AUTH" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"Brain: auth failed. Call ss(namespace=$NAMESPACE, action=start, session_id=$SESSION_ID, project_path=$PWD, goals=infer from context) manually.\"}}"
  rm -f "$HEADERS_TMP"
  exit 0
fi

INIT=$(curl -s --max-time 8 -D "$HEADERS_TMP" \
  -H "Authorization: $AUTH" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$BRAIN_MCP_URL" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"brain-hook","version":"1.0"}},"id":0}' 2>/dev/null)

MCP_SID=$(grep -i 'mcp-session-id' "$HEADERS_TMP" 2>/dev/null | tr -d '\r' | awk '{print $2}')
rm -f "$HEADERS_TMP"

if [ -z "$MCP_SID" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"Brain: MCP init failed against $BRAIN_MCP_URL. Call ss(namespace=$NAMESPACE, action=start, session_id=$SESSION_ID, project_path=$PWD, goals=infer from context) manually.\"}}"
  exit 0
fi

curl -s --max-time 3 \
  -H "Authorization: $AUTH" \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $MCP_SID" \
  -X POST "$BRAIN_MCP_URL" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null 2>&1

CALL_BODY=$(NAMESPACE="$NAMESPACE" SESSION_ID="$SESSION_ID" PWD_VAL="$PWD" python3 <<'PYEOF' 2>/dev/null
import json, os
print(json.dumps({
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
        "name": "ss",
        "arguments": {
            "namespace": os.environ["NAMESPACE"],
            "action": "start",
            "session_id": os.environ["SESSION_ID"],
            "project_path": os.environ["PWD_VAL"],
            "goals": "new session - goals inferred from first user message"
        }
    },
    "id": 1
}))
PYEOF
)

RESULT=$(curl -s --max-time 12 \
  -H "Authorization: $AUTH" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $MCP_SID" \
  -X POST "$BRAIN_MCP_URL" \
  -d "$CALL_BODY" 2>/dev/null)

PARSED=$(python3 -c "
import json, sys
try:
    r = json.loads(sys.stdin.read())
    content = r.get('result',{}).get('content',[{}])[0].get('text','')
    data = json.loads(content) if content else {}
    brief = data.get('context_brief','') or ''
    needs_prompt = bool(data.get('context_size_prompt_needed'))
    print(json.dumps({'brief': brief, 'needs_prompt': needs_prompt}))
except Exception:
    print(json.dumps({'brief': '', 'needs_prompt': False}))
" <<< "$RESULT" 2>/dev/null)

CONTEXT=$(python3 -c "
import json, sys
p = json.loads(sys.stdin.read() or '{}')
print(json.dumps(p.get('brief',''))[1:-1])
" <<< "$PARSED" 2>/dev/null)

NEEDS_PROMPT=$(python3 -c "
import json, sys
p = json.loads(sys.stdin.read() or '{}')
print('1' if p.get('needs_prompt') else '0')
" <<< "$PARSED" 2>/dev/null)

PROMPT_INSTRUCTION=""
if [ "$NEEDS_PROMPT" = "1" ]; then
  PROMPT_INSTRUCTION="\\n\\n---\\n**FIRST-TIME SETUP for namespace \`$NAMESPACE\`:** ask the user how much project context to load on session start. Options: 0=none, 1=light (1000 tokens), 2=medium (2000), 3=heavy (5000), or a custom integer 1..20000. Then call ss(action=\\\"start\\\", namespace=\\\"$NAMESPACE\\\", session_id=\\\"$SESSION_ID\\\", project_path=\\\"$PWD\\\", goals=\\\"<resume from first message>\\\", context_size=<their choice>) — the choice is persisted on the namespace profile and reused for future sessions."
fi

if [ -n "$CONTEXT" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"Brain session active (ns=$NAMESPACE, sid=$SESSION_ID). Prior context loaded automatically:\\n\\n$CONTEXT$PROMPT_INSTRUCTION\"}}"
else
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"Brain session active (ns=$NAMESPACE, sid=$SESSION_ID). No prior context found for this project.$PROMPT_INSTRUCTION\"}}"
fi

exit 0
