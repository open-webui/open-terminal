#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT_DIR/systemd/open-terminal.service"
UNIT_DST="$HOME/.config/systemd/user/open-terminal.service"

mkdir -p "$HOME/.config/systemd/user"
cp "$UNIT_SRC" "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable open-terminal.service

echo "Installed user service: $UNIT_DST"
echo "Start with: systemctl --user start open-terminal"
