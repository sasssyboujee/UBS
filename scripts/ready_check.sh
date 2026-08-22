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
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"navigate","arguments":{"map_id":"gAAAAABqiRi6wK6JieL40yLCnrNMcbPcHQcorviBv8fmWThjzuLkHN26Z6QFjpqIQDFJzt8cPOwbNKe0WQcQM7QurbhI0Nx9kA==","from_node":"N11","to":"N04"}}}')
case "$NAV_OUT" in
  *'"text":"N07"'*) echo "  ok: navigate returns N07 for the known map" ;;
  *) echo "FAIL: navigate did not return N07 (got: $(echo "$NAV_OUT" | head -c 160))"; fail=1 ;;
esac

# 5) Phase 3: venues open on Monday at 20:00.
if curl -fsS -m 20 -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"find_open_venues","arguments":{"day":"Monday","time":"20:00"}}}' \
  | grep -q 'Nine Quarters'; then
  echo "  ok: find_open_venues returns Nine Quarters"
else
  echo "FAIL: find_open_venues did not answer (deploy may not be live yet)"
  fail=1
fi

# 6) Phase 3: meeting window for ada on Tuesday 13:00-18:00.
if curl -fsS -m 20 -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"find_meeting_window","arguments":{"day":"Tuesday","start":"13:00","end":"18:00","duration_minutes":60,"people":["ada"]}}}' \
  | grep -q '14:00-15:00'; then
  echo "  ok: find_meeting_window returns 14:00-15:00"
else
  echo "FAIL: find_meeting_window did not answer (deploy may not be live yet)"
  fail=1
fi

if [ "$fail" = "0" ]; then
  echo "READY - safe to submit an evaluation."
else
  echo "NOT READY - fix the failures above before evaluating."
fi
exit "$fail"
