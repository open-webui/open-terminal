#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 \"<command>\" [--force]"
  exit 1
fi

CMD="$1"
FORCE="${2:-}"

if [[ "$PWD" != /home/trotsky/Projects* ]]; then
  echo "Refusing to run outside /home/trotsky/Projects"
  exit 1
fi

if [[ "$CMD" =~ (^|[[:space:]])(rm[[:space:]]+-rf|mkfs|dd[[:space:]]+if=|shutdown|reboot|poweroff)($|[[:space:]]) ]] && [[ "$FORCE" != "--force" ]]; then
  echo "Blocked risky command. Re-run with --force if explicitly approved."
  exit 1
fi

if [[ "$CMD" =~ /etc|/usr|/var ]] && [[ "$FORCE" != "--force" ]]; then
  echo "Blocked system-path edit/access command. Re-run with --force if explicitly approved."
  exit 1
fi

echo "[$(date -Is)] $CMD" >> "${XDG_STATE_HOME:-$HOME/.local/state}/open-terminal/guarded-run.log"
bash -lc "$CMD"
