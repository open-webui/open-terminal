#!/usr/bin/env bash
set -euo pipefail
systemctl --user start open-terminal
systemctl --user --no-pager status open-terminal
