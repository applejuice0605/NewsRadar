#!/bin/zsh
set -eu
cd /Users/bruce/Coding/NewsRadar
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LOCK_DIR=/tmp/newsradar-dashboard-monitor.lock
if ! mkdir "$LOCK_DIR" 2>/dev/null; then exit 0; fi
trap 'rmdir "$LOCK_DIR"' EXIT
python3 scripts/monitor_dashboard.py "$@"
