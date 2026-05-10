#!/usr/bin/env bash
set -euo pipefail
journalctl --user -u open-terminal -n "${1:-200}" --no-pager
