#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT_DIR/scripts/codex-gemma"
DST_DIR="$HOME/.local/bin"
DST="$DST_DIR/codex-gemma"

if [[ "${CODEX_GEMMA_ALLOW_HISTORICAL:-0}" != "1" ]]; then
  cat <<'EOF'
Retired: codex-gemma is no longer installed as an active Open Terminal operational path.
Use packet-mediated local-worker delegation instead.

If you need the historical wrapper for a benchmark or migration audit, rerun with:
  CODEX_GEMMA_ALLOW_HISTORICAL=1 scripts/install-codex-gemma-cli.sh

If you later want to remove the historical wrapper entirely:
  rm -f "$HOME/.local/bin/codex-gemma"
EOF
  exit 1
fi

mkdir -p "$DST_DIR"
chmod +x "$SRC"
ln -sfn "$SRC" "$DST"

echo "Installed: $DST"
echo "Historical use only: CODEX_GEMMA_ALLOW_HISTORICAL=1 codex-gemma <args>"
