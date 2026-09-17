#!/bin/bash
# Brain Memory System — Session Start Hook
# 1. Sets BRAIN_NAMESPACE and BRAIN_SESSION_ID env vars (visible to all subsequent hooks)
# 2. Calls Brain MCP directly to start the session
# 3. Injects context_brief into Claude's initial context
#
# No configuration: auth reuses the Brain login Claude Code holds for the
# plugin's MCP server (see bin/brain-hook-auth). If that login is missing or
# can't be refreshed, the hook says exactly that and tells the AI to call
# ss() itself.

set -uo pipefail

NAMESPACE="$(basename "$PWD")"
SESSION_ID="session:$(date +%s)-${NAMESPACE}"

# Propagate env to every later hook (PreToolUse, PostToolUse, Stop)
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export BRAIN_NAMESPACE=$NAMESPACE" >> "$CLAUDE_ENV_FILE"
  echo "export BRAIN_SESSION_ID=$SESSION_ID" >> "$CLAUDE_ENV_FILE"
fi

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/local/brain}"
MANUAL_START="Call ss(namespace=$NAMESPACE, action=start, session_id=$SESSION_ID, project_path=$PWD, goals=infer from context) manually."

# Emit SessionStart additionalContext with proper JSON escaping.
emit_context() {
  CTX="$1" python3 -c 'import json,os; print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":os.environ["CTX"]}}))'
}

AUTH_JSON=$(python3 "$PLUGIN_ROOT/bin/brain-hook-auth" 2>/dev/null)
AUTH=$(echo "$AUTH_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('authorization',''))" 2>/dev/null || echo "")
BRAIN_MCP_URL=$(echo "$AUTH_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mcp_url',''))" 2>/dev/null || echo "")

if [ -z "$AUTH" ] || [ -z "$BRAIN_MCP_URL" ]; then
  REASON=$(echo "$AUTH_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
  emit_context "Brain hooks: ${REASON:-could not get a token from your Brain login}. $MANUAL_START"
  exit 0
fi

HEADERS_TMP=$(mktemp)
INIT=$(curl -s --max-time 8 -D "$HEADERS_TMP" \
  -H "Authorization: $AUTH" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$BRAIN_MCP_URL" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"brain-hook","version":"1.0"}},"id":0}' 2>/dev/null)

MCP_SID=$(grep -i 'mcp-session-id' "$HEADERS_TMP" 2>/dev/null | tr -d '\r' | awk '{print $2}')
rm -f "$HEADERS_TMP"

if [ -z "$MCP_SID" ]; then
  emit_context "Brain: MCP init failed against $BRAIN_MCP_URL. $MANUAL_START"
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
