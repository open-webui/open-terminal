#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT_DIR/systemd/open-terminal.service"
UNIT_DST="$HOME/.config/systemd/user/open-terminal.service"
VERIFY_SERVICE_SRC="$ROOT_DIR/systemd/open-terminal-verify.service"
VERIFY_SERVICE_DST="$HOME/.config/systemd/user/open-terminal-verify.service"
VERIFY_TIMER_SRC="$ROOT_DIR/systemd/open-terminal-verify.timer"
VERIFY_TIMER_DST="$HOME/.config/systemd/user/open-terminal-verify.timer"

mkdir -p "$HOME/.config/systemd/user"
cp "$UNIT_SRC" "$UNIT_DST"
cp "$VERIFY_SERVICE_SRC" "$VERIFY_SERVICE_DST"
cp "$VERIFY_TIMER_SRC" "$VERIFY_TIMER_DST"

systemctl --user daemon-reload
systemctl --user enable open-terminal.service
systemctl --user enable open-terminal-verify.timer

echo "Installed user service: $UNIT_DST"
echo "Installed verify units: $VERIFY_SERVICE_DST, $VERIFY_TIMER_DST"
echo "Start with: systemctl --user start open-terminal"
