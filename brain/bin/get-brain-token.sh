#!/bin/bash
# Brain MCP token helper — Keycloak client_credentials with offline token refresh.
#
# First call: fetches access token + offline refresh token via client_credentials.
# Subsequent calls: returns cached access token, refreshing via offline token when expired.
# The offline refresh token never expires — users configure once, works forever.
#
# Required env vars:
#   KEYCLOAK_TOKEN_URL     — Keycloak token endpoint
#   KEYCLOAK_CLIENT_ID     — Brain client ID (e.g. brain-goldfish-paul-gmail-com)
#   KEYCLOAK_CLIENT_SECRET — Brain client secret

set -euo pipefail

TOKEN_URL="${KEYCLOAK_TOKEN_URL:?KEYCLOAK_TOKEN_URL not set}"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:?KEYCLOAK_CLIENT_ID not set}"
CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:?KEYCLOAK_CLIENT_SECRET not set}"

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/brain-mcp"
CACHE_FILE="$CACHE_DIR/token-${CLIENT_ID}.json"
mkdir -p "$CACHE_DIR"
chmod 700 "$CACHE_DIR"

now=$(date +%s)

# --- Try cached token ---
if [ -f "$CACHE_FILE" ]; then
  cached=$(python3 -c "
import json, sys
try:
    d = json.load(open('$CACHE_FILE'))
    print(json.dumps(d))
except:
    print('{}')
" 2>/dev/null)

  expires_at=$(echo "$cached" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expires_at', 0))" 2>/dev/null || echo 0)
  access_token=$(echo "$cached" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token', ''))" 2>/dev/null || echo "")
  refresh_token=$(echo "$cached" | python3 -c "import sys,json; print(json.load(sys.stdin).get('refresh_token', ''))" 2>/dev/null || echo "")

  # Still valid (30s buffer)
  if [ -n "$access_token" ] && [ "$now" -lt "$((expires_at - 30))" ]; then
    echo "{\"Authorization\": \"Bearer $access_token\"}"
    exit 0
  fi

  # Expired but have refresh token — use it
  if [ -n "$refresh_token" ]; then
    RESPONSE=$(curl -s --max-time 8 -X POST "$TOKEN_URL" \
      -d "client_id=$CLIENT_ID" \
      -d "client_secret=$CLIENT_SECRET" \
      -d "grant_type=refresh_token" \
      -d "refresh_token=$refresh_token" 2>/dev/null)

    new_access=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")
    new_refresh=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('refresh_token',''))" 2>/dev/null || echo "")
    new_expires_in=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expires_in', 0))" 2>/dev/null || echo 0)

    if [ -n "$new_access" ] && [ "$new_access" != "" ]; then
      # Refresh may rotate the offline token — always save the latest
      save_refresh="${new_refresh:-$refresh_token}"
      python3 -c "
import json
json.dump({
    'access_token': '''$new_access''',
    'refresh_token': '''$save_refresh''',
    'expires_at': $now + $new_expires_in
}, open('$CACHE_FILE', 'w'))
" 2>/dev/null
      chmod 600 "$CACHE_FILE"
      echo "{\"Authorization\": \"Bearer $new_access\"}"
      exit 0
    fi
    # Refresh failed — fall through to full client_credentials
  fi
fi

# --- No cache or refresh failed — full client_credentials grant ---
RESPONSE=$(curl -s --max-time 8 -X POST "$TOKEN_URL" \
  -d "client_id=$CLIENT_ID" \
  -d "client_secret=$CLIENT_SECRET" \
  -d "grant_type=client_credentials" \
  -d "scope=offline_access" 2>/dev/null)

access_token=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")
refresh_token=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('refresh_token',''))" 2>/dev/null || echo "")
expires_in=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expires_in', 300))" 2>/dev/null || echo 300)

if [ -z "$access_token" ]; then
  echo "{\"error\": \"token fetch failed\"}" >&2
  exit 1
fi

python3 -c "
import json
json.dump({
    'access_token': '''$access_token''',
    'refresh_token': '''$refresh_token''',
    'expires_at': $now + $expires_in
}, open('$CACHE_FILE', 'w'))
" 2>/dev/null
chmod 600 "$CACHE_FILE"

echo "{\"Authorization\": \"Bearer $access_token\"}"
