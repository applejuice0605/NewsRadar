#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
REPO_DIR="${REPO_DIR:-${SCRIPT_DIR:h}}"
# Replay a completed frozen edition through the same launchd shell entry.
if [[ "${1:-}" == "--rehearse" ]]; then
  export PYTHONPATH="$REPO_DIR/TrendRadar${PYTHONPATH:+:$PYTHONPATH}"
  exec "$REPO_DIR/TrendRadar/.venv/bin/python" "$REPO_DIR/scripts/rehearse_evening_edition.py" --date "${2:?rehearsal date required}" --publish
fi
# Ensure final analysis has a run near the cutoff even when hourly StartInterval
# has drifted. The existing collector lock prevents overlapping collection.
if ! "$REPO_DIR/scripts/run_trendradar_hourly.sh"; then
  echo "[edition] collection/publication failed; attempting podcast independently from validated frozen facts"
fi
export PYTHONPATH="$REPO_DIR/TrendRadar${PYTHONPATH:+:$PYTHONPATH}"
exec "$REPO_DIR/TrendRadar/.venv/bin/python" "$REPO_DIR/scripts/run_daily_podcast.py" --publish
