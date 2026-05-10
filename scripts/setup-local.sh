#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/open-terminal"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/open-terminal"

mkdir -p "$CONFIG_DIR" "$STATE_DIR/logs"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  uv venv "$VENV_DIR"
fi

uv pip install --python "$VENV_DIR/bin/python" -e "$ROOT_DIR"

if [[ ! -f "$CONFIG_DIR/api_key" ]]; then
  umask 077
  python3 - <<'PY'
import secrets
from pathlib import Path
path = Path.home() / ".config" / "open-terminal" / "api_key"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
PY
  chmod 600 "$CONFIG_DIR/api_key"
fi

{
  echo -n "OPEN_TERMINAL_API_KEY="
  tr -d '\r\n' < "$CONFIG_DIR/api_key"
  echo
} > "$CONFIG_DIR/api_key.env"
chmod 600 "$CONFIG_DIR/api_key.env"

echo "Local setup complete."
echo "Next: scripts/install-user-service.sh"
