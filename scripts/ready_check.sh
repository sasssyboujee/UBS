#!/usr/bin/env bash
# Tool-box pre-evaluation ready check.
#
# Run this BEFORE submitting an evaluation. It verifies the deployed server
# answers the exact calls the challenge android makes, so a deploying or
# cold-starting Render service can be caught here instead of inside a scored
# run. Usage: bash scripts/ready_check.sh
set -u

BASE="${TEAM_URL:-https://ubs-coding-challenge-api.onrender.com}"
fail=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  ok: $label"
  else
    echo "FAIL: $label"
    fail=1
  fi
}

echo "Ready check against $BASE"

# 1) Service is up and not sleeping.
check "GET /healthz returns 200" curl -fsS -m 15 "$BASE/healthz"

# 2) MCP initialize answers.
check "MCP initialize" curl -fsS -m 15 -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# 3) recall answers with the official study set and stays inside 10s.
if curl -fsS -m 20 -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"retrieve","arguments":{"question":"When did the board formally approve the arrangement for sharing the drying machine?"}}}' \
  | grep -q '21 May'; then
  echo "  ok: retrieve returns the 21 May fact"
else
  echo "FAIL: retrieve did not return the 21 May fact (deploy may not be live yet)"
  fail=1
fi

# 4) navigate answers with a next node (not an error) for a known map.
NAV_OUT=$(curl -fsS -m 20 -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"navigate","arguments":{"map_id":"gAAAAABqiRi6wK6JieL40yLCnrNMcbPcHQcorviBv8fmWThjzuLkHN26Z6QFjpqIQDFJzt8cPOwbNKe0WQcQM7QurbhI0Nx9kA==","from":"N11","to":"N04"}}}')
case "$NAV_OUT" in
  *'"text":"N07"'*) echo "  ok: navigate returns N07 for the known map" ;;
  *) echo "FAIL: navigate did not return N07 (got: $(echo "$NAV_OUT" | head -c 160))"; fail=1 ;;
esac

if [ "$fail" = "0" ]; then
  echo "READY - safe to submit an evaluation."
else
  echo "NOT READY - fix the failures above before evaluating."
fi
exit "$fail"
