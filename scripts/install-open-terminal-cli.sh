#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT_DIR/scripts/open-terminal"
DST_DIR="$HOME/.local/bin"
DST="$DST_DIR/open-terminal"

mkdir -p "$DST_DIR"
chmod +x "$SRC"
ln -sfn "$SRC" "$DST"

echo "Installed: $DST"
echo "Run: open-terminal"
