#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop open-terminal
systemctl --user --no-pager status open-terminal || true
