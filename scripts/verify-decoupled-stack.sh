#!/usr/bin/env bash
set -euo pipefail

PASS_COUNT=0
FAIL_COUNT=0

pass() {
  echo "PASS: $1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
  echo "FAIL: $1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

check_systemd() {
  if systemctl --user is-active --quiet open-terminal; then
    pass "open-terminal.service is active"
  else
    fail "open-terminal.service is not active"
  fi
}

check_open_terminal_health() {
  local out
  if out="$(curl -fsS http://127.0.0.1:8010/health 2>/dev/null)" && [[ "$out" == *'"status":"ok"'* ]]; then
    pass "OpenTerminal health endpoint is OK"
  else
    fail "OpenTerminal health endpoint failed"
  fi
}

check_open_terminal_execute() {
  local key id status_json
  key="$(tr -d '\r\n' < "${XDG_CONFIG_HOME:-$HOME/.config}/open-terminal/api_key")"

  id="$(
    curl -fsS -X POST http://127.0.0.1:8010/execute \
      -H "Authorization: Bearer ${key}" \
      -H 'Content-Type: application/json' \
      -d '{"command":"pwd && echo verify-decoupled-stack-ok","background":false}' \
      | sed -n 's/.*"id":"\([^"]*\)".*/\1/p'
  )"

  if [[ -z "$id" ]]; then
    fail "OpenTerminal execute did not return process id"
    return
  fi

  status_json="$(
    curl -fsS \
      -H "Authorization: Bearer ${key}" \
      "http://127.0.0.1:8010/execute/${id}/status?wait=8"
  )"

  if [[ "$status_json" == *'"status":"done"'* && "$status_json" == *'"exit_code":0'* && "$status_json" == *'verify-decoupled-stack-ok'* ]]; then
    pass "OpenTerminal execute/status flow works"
  else
    fail "OpenTerminal execute/status flow failed"
  fi
}

check_openwebui_health() {
  local out
  if out="$(curl -fsS http://127.0.0.1:8080/health 2>/dev/null)" && [[ "$out" == *'"status":true'* ]]; then
    pass "OpenWebUI health endpoint is OK"
  else
    fail "OpenWebUI health endpoint failed"
  fi
}

check_openwebui_decoupled() {
  local env_dump
  if ! docker ps --format '{{.Names}}' | rg -x 'open-webui' >/dev/null; then
    fail "OpenWebUI container not running"
    return
  fi

  env_dump="$(docker inspect open-webui --format '{{json .Config.Env}}')"
  if [[ "$env_dump" == *"TERMINAL_SERVER_CONNECTIONS"* ]]; then
    fail "OpenWebUI still has TERMINAL_SERVER_CONNECTIONS"
  else
    pass "OpenWebUI is decoupled from OpenTerminal env config"
  fi
}

echo "Running decoupled stack verification..."
check_systemd
check_open_terminal_health
check_open_terminal_execute
check_openwebui_health
check_openwebui_decoupled

echo
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
