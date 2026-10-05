#!/usr/bin/env bash
# A guided run through the lean-pool demo: what the pool does with a repeated check, a wrong
# proof, a Lean timeout, a Lean server that goes away and a cache that goes away.
#
#   docker compose -f examples/demo/compose.yaml up --build -d
#   examples/demo/walkthrough.sh
#
# Every step prints what it sent and what came back. The script stops with a non-zero status at
# the first step that did not behave as described, and the project's CI runs it for that reason.
# It needs bash, curl and awk, and can be run again against the same pool: every run puts a
# line of its own into the code it sends, so nothing an earlier run stored is in its way.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
POOL=${LEANPOOL_DEMO_URL:-http://127.0.0.1:18100}
KEY=${LEANPOOL_DEMO_API_KEY:-demo-key}
# How the demo's services are stopped and started. Set LEANPOOL_DEMO_COMPOSE to a command of your
# own to walk through a pool that was started some other way.
if [ -n "${LEANPOOL_DEMO_COMPOSE:-}" ]; then
  read -r -a COMPOSE <<< "$LEANPOOL_DEMO_COMPOSE"
else
  COMPOSE=(docker compose -f "$HERE/compose.yaml")
fi

step() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
fail() { printf '\nwalkthrough: %s\n' "$*" >&2; exit 1; }

# check ID CODE: send one check. Sets BODY, STATUS and SECONDS_TAKEN, and prints them.
check() {
  local reply
  reply=$(curl -sS -m 120 -w '\n%{http_code} %{time_total}' "$POOL/api/check" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d "{\"snippets\": [{\"id\": \"$1\", \"code\": \"$2\"}], \"timeout\": 60}") \
    || fail "the pool did not answer the check $1"
  BODY=${reply%$'\n'*}
  read -r STATUS SECONDS_TAKEN <<< "${reply##*$'\n'}"
  note "HTTP $STATUS in ${SECONDS_TAKEN} s"
  note "$BODY"
}

expect_status() { [ "$STATUS" = "$1" ] || fail "expected HTTP $1, got $STATUS"; }
expect_in_body() { case $BODY in *"$1"*) ;; *) fail "expected the answer to contain: $1" ;; esac; }
expect_not_in_body() { case $BODY in *"$1"*) fail "the answer should not contain: $1" ;; esac; }
expect_faster_than() {
  awk -v took="$SECONDS_TAKEN" -v limit="$1" 'BEGIN { exit !(took < limit) }' \
    || fail "expected an answer within $1 s, it took $SECONDS_TAKEN s"
}

# wait_for PATH TEXT SECONDS: wait until GET PATH answers with a body that contains TEXT.
wait_for() {
  local deadline=$((SECONDS + $3)) body=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    body=$(curl -s -m 5 "$POOL$1" -H "Authorization: Bearer $KEY" || true)
    case $body in
      *"$2"*) note "GET $1 -> $body"; return 0 ;;
      *"API key"*) fail "the pool refused the API key (LEANPOOL_DEMO_API_KEY): $body" ;;
    esac
    sleep 1
  done
  fail "GET $1 did not show $2 within $3 s (last answer: ${body:-none})"
}

# One comment line that no earlier run sent ("\n" is a line break once it is inside JSON).
RUN="-- walkthrough $(date +%s)-$$\n"
TWO="${RUN}theorem two : 1 + 1 = 2 := by rfl"
WRONG="${RUN}theorem hard : 2 + 2 = 5 := by sorry"
SLOW="${RUN}-- standin: timeout\ntheorem slow : True := by trivial"

step "1. The pool is up: one address, two Lean servers, six workers"
wait_for /health '"workers":6' 120
# The proxy uses the cache once it has seen it healthy, a second or two after both started.
# Until then checks are answered all the same, uncached (step 7 shows that on purpose).
wait_for /status '"stored_entries"' 60
note "every answer also carries the pool's size in its headers:"
HEADERS=$(curl -s -m 5 -D - -o /dev/null "$POOL/health" | tr -d '\r' | grep -i '^x-lean-pool-') \
  || fail "the answer carries no X-Lean-Pool headers"
printf '%s\n' "$HEADERS" | sed 's/^/   /'

step "2. A check. Nothing is stored yet, so a Lean server works on it"
check attempt-1 "$TWO"
expect_status 200
expect_in_body "accepted without running Lean"
expect_not_in_body '"cached"'

step "3. The same proof as another attempt: answered from the cache, under the new id"
check attempt-2 "$TWO"
expect_status 200
expect_in_body '"id": "attempt-2"'
expect_in_body '"cached": true'
expect_faster_than 1.0

step "4. A proof Lean rejects is a verdict too, and is stored like one"
check wrong-1 "$WRONG"
expect_in_body "declaration uses 'sorry'"
expect_not_in_body '"cached"'
check wrong-2 "$WRONG"
expect_in_body '"cached": true'

step "5. A Lean timeout is no verdict: it is passed on as it is, and never stored"
check slow-1 "$SLOW"
expect_status 200
expect_in_body "timed out"
check slow-2 "$SLOW"
expect_in_body "timed out"
expect_not_in_body '"cached"'

step "6. A Lean server goes away. The check that meets it is sent to the other one"
"${COMPOSE[@]}" stop agent-b lean-b
check after-loss "${RUN}theorem three : 1 + 2 = 3 := by rfl"
expect_status 200
expect_in_body "stand-in lean-a"
wait_for /health '"servers":1' 60

step "7. The cache goes away. Checks keep flowing, straight to a Lean server"
"${COMPOSE[@]}" stop cache
check without-cache "$TWO"
expect_status 200
expect_in_body "accepted without running Lean"
expect_not_in_body '"cached"'
wait_for /status "the cache is down" 30

step "8. Both come back. The cache kept its store, and the pool its size"
"${COMPOSE[@]}" start lean-b
"${COMPOSE[@]}" start agent-b cache
wait_for /health '"workers":6' 120
wait_for /status '"stored_entries"' 60
check after-return "$TWO"
expect_in_body '"cached": true'

step "9. What the cache counted since it came back"
wait_for /status '"hits": 1' 10

printf '\nEvery step behaved as described. Stop the demo with:\n'
printf '   docker compose -f examples/demo/compose.yaml down -v\n'
