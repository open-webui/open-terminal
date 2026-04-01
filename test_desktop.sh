#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  Integration tests for the virtual desktop ("Computer Use") feature.
#
#  Expects the container to be already running with:
#    BASE_URL  – e.g. http://localhost:8000
#    API_KEY   – the bearer token
# ---------------------------------------------------------------------------
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
API_KEY="${API_KEY:-test-secret-key}"

PASS=0
FAIL=0

# -- helpers ----------------------------------------------------------------

api() {
  local method="$1" path="$2"
  shift 2
  curl -sf -X "$method" \
    "${BASE_URL}${path}" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    "$@" 2>/dev/null
}

api_raw() {
  local method="$1" path="$2"
  shift 2
  curl -s -X "$method" \
    "${BASE_URL}${path}" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -o /dev/null -w '%{http_code}' \
    "$@" 2>/dev/null
}

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected=$expected actual=$actual)"
    FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -q "$needle"; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (response did not contain '$needle')"
    FAIL=$((FAIL + 1))
  fi
}

assert_file_png() {
  local label="$1" file="$2"
  local magic
  magic=$(xxd -l4 -p "$file" 2>/dev/null || echo "")
  if [ "$magic" = "89504e47" ]; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (not a valid PNG, magic=$magic)"
    FAIL=$((FAIL + 1))
  fi
}

# -- tests ------------------------------------------------------------------

echo "=== Test 1: Health check ==="
health=$(api GET /health)
assert_contains "health returns ok" '"status":"ok"' "$health"

echo ""
echo "=== Test 2: Config includes desktop feature ==="
config=$(api GET /api/config)
assert_contains "desktop feature flag present" '"desktop":true' "$config"

echo ""
echo "=== Test 3: Desktop status (before start) ==="
status=$(api GET /desktop)
assert_contains "desktop reports not running" '"running":false' "$status"

echo ""
echo "=== Test 4: Screenshot fails when desktop not running ==="
code=$(api_raw POST /desktop/screenshot)
assert_eq "screenshot returns 503" "503" "$code"

echo ""
echo "=== Test 5: Start the desktop ==="
status=$(api POST /desktop/start)
sleep 3
assert_contains "desktop started" '"running":true' "$status"

echo ""
echo "=== Test 6: Desktop status (after start) ==="
status=$(api GET /desktop)
assert_contains "desktop running" '"running":true' "$status"
assert_contains "screen width present" '"screen_width"' "$status"
assert_contains "screen height present" '"screen_height"' "$status"

echo ""
echo "=== Test 7: Screenshot (base64 JSON) ==="
resp=$(api POST /desktop/screenshot)
assert_contains "screenshot has width" '"width"' "$resp"
assert_contains "screenshot has height" '"height"' "$resp"
assert_contains "screenshot has data" '"data"' "$resp"
assert_contains "screenshot format is png" '"format":"png"' "$resp"

echo ""
echo "=== Test 8: Screenshot (raw binary PNG) ==="
tmp_raw="/tmp/desktop_screenshot_raw.png"
api POST "/desktop/screenshot?format=raw" -o "$tmp_raw" >/dev/null 2>&1 || true
assert_file_png "raw screenshot is valid PNG" "$tmp_raw"

echo ""
echo "=== Test 9: Mouse click ==="
resp=$(api POST /desktop/click -d '{"x":100,"y":100,"button":1}')
assert_contains "click returns ok" '"status":"ok"' "$resp"

echo ""
echo "=== Test 10: Mouse move ==="
resp=$(api POST /desktop/mouse_move -d '{"x":200,"y":200}')
assert_contains "mouse_move returns ok" '"status":"ok"' "$resp"

echo ""
echo "=== Test 11: Type text ==="
resp=$(api POST /desktop/type -d '{"text":"hello world"}')
assert_contains "type returns ok" '"status":"ok"' "$resp"

echo ""
echo "=== Test 12: Key press ==="
resp=$(api POST /desktop/key -d '{"key":"Return"}')
assert_contains "key returns ok" '"status":"ok"' "$resp"

echo ""
echo "=== Test 13: Complex key combo ==="
resp=$(api POST /desktop/key -d '{"key":"ctrl+a"}')
assert_contains "key combo returns ok" '"status":"ok"' "$resp"

echo ""
echo "=== Test 14: Scroll ==="
resp=$(api POST /desktop/scroll -d '{"x":640,"y":360,"direction":"down","amount":3}')
assert_contains "scroll returns ok" '"status":"ok"' "$resp"

echo ""
echo "=== Test 15: Drag ==="
resp=$(api POST /desktop/drag -d '{"start_x":100,"start_y":100,"end_x":300,"end_y":300,"button":1}')
assert_contains "drag returns ok" '"status":"ok"' "$resp"

echo ""
echo "=== Test 16: Second screenshot (after interactions) ==="
tmp_raw2="/tmp/desktop_screenshot_raw2.png"
api POST "/desktop/screenshot?format=raw" -o "$tmp_raw2" >/dev/null 2>&1 || true
assert_file_png "second screenshot is valid PNG" "$tmp_raw2"

echo ""
echo "=== Test 17: Stop the desktop ==="
status=$(api POST /desktop/stop)
assert_contains "desktop stopped" '"running":false' "$status"

echo ""
echo "=== Test 18: Desktop status after stop ==="
status=$(api GET /desktop)
assert_contains "desktop not running after stop" '"running":false' "$status"

echo ""
echo "=== Test 19: Screenshot fails after stop ==="
code=$(api_raw POST /desktop/screenshot)
assert_eq "screenshot returns 503 after stop" "503" "$code"

echo ""
echo "=== Test 20: Restart desktop (idempotent start) ==="
status=$(api POST /desktop/start)
sleep 3
assert_contains "desktop restarted" '"running":true' "$status"
status2=$(api POST /desktop/start)
assert_contains "second start is idempotent" '"running":true' "$status2"

echo ""
echo "=== Test 21: Config endpoint with desktop feature ==="
config=$(api GET /api/config)
assert_contains "desktop in config" '"desktop"' "$config"

# -- cleanup --
rm -f /tmp/desktop_screenshot_raw.png /tmp/desktop_screenshot_raw2.png

echo ""
echo "==========================================="
echo "  Results: $PASS passed, $FAIL failed"
echo "==========================================="

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
